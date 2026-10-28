#!/usr/bin/env python3
"""Homeboy watchdog: checks every app and reports to Healthchecks.io (stdlib only).

Healthchecks.io lives outside the Pi, so it also notices when the Pi, its power or the home
internet is gone: it alerts when the pings stop. Every minute this script tests each group below
and then
  - all tests pass            -> pings the group's check ("still alive"),
  - a test failed twice in a row -> pings <url>/fail with the reasons (instant alert, reasons in the message),
  - a test failed once         -> stays quiet (a restart during a deploy must not wake anybody up).

    watchdog.py              run all groups and ping
    watchdog.py --dry-run    run all groups, print the result, ping nothing
    watchdog.py ping KEY     send a success ping for KEY (used by the backup scripts)
    watchdog.py maintenance on|off KEY   pause a group while its hardware is being worked on (auto-expires after 12 h)
"""

import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECKS_FILE = HERE / "checks.json"   # group key -> ping URL (written by setup_checks.py, secret)
STATE_FILE = HERE / "state.json"     # consecutive failures per group
FAILS_BEFORE_ALERT = 2
MAINT_DIR = HERE / "maintenance"      # one empty file per group key = "being worked on, do not alarm"
MAINT_MAX_HOURS = 12                  # a forgotten flag must not silence a group for good
SMART_DIR = HERE / "smart"            # per disk: last SMART result and the error counters seen so far
SMART_EVERY = 3600                    # read SMART once an hour; in between the last result counts
# raw counters that only ever grow when the disk gets worse: any rise is one alarm (then the new value is the baseline)
SMART_GROWING = {5: "defekte Sektoren (ersetzt)", 187: "nicht korrigierbare Fehler", 196: "Sektor-Ersetzungen",
                 197: "wackelige Sektoren", 198: "unlesbare Sektoren", 178: "Reserveblöcke verbraucht",
                 181: "Schreibfehler", 182: "Löschfehler"}
USER_AGENT = "homeboy-watchdog/1"

# Test specs: ("unit", name) ("http", url, ok) ("tcp", port) ("disk", path, max_percent) ("temp", max_celsius)
# ("mem", min_available_percent) ("media", folder: mounted disk answers and is not empty)
# ("smart", /dev/disk/by-id/..., label, max_celsius: SMART health, growing error counters, temperature). For "http", ok is a set of status codes or "<500" (any answer from the app).
GROUPS = {
    "wagerl": ("Wagerl", [
        ("unit", "wagerl"),
        ("http", "http://127.0.0.1:8220/health", {200}),
        ("http", "https://wagerl.anmibu.com/health", {200}),
    ]),
    "buchnancials": ("Buchnancials", [
        ("unit", "buchnancials"),
        ("http", "http://127.0.0.1:8200/health", {200}),
        ("http", "https://buchnancials.anmibu.com/health", {200}),
    ]),
    "games": ("Games", [
        ("unit", "games"),
        ("http", "http://127.0.0.1:8210/", {200}),
        ("http", "https://games.anmibu.com/", {200}),
    ]),
    "tourdesalon": ("Tour de Salon", [
        ("unit", "tourdesalon"),
        ("http", "http://127.0.0.1:8230/api/health", {200}),
        # öffentliche Adresse kommt dazu, sobald der DNS-Eintrag tourdesalon.anmibu.com steht
    ]),
    "mcp": ("MCP-Server", [
        ("unit", "mcp-gateway"), ("unit", "mcp-gateway-site"), ("unit", "epaper-backend"),
        ("unit", "intervals-icu-backend"), ("unit", "system-status-backend"),
        ("http", "http://127.0.0.1:8001/", "<500"),       # gateway: answers 404/401, any answer means alive
        ("tcp", 8000), ("tcp", 8100), ("tcp", 8101),       # intervals.icu, e-paper, system status (no health URL)
        ("http", "https://mcp.anmibu.com/", "<500"),
    ]),
    "jellyfin": ("Jellyfin (Medien)", [
        ("http", "http://127.0.0.1:8096/health", {200}),   # Docker container, no user unit to ask
        ("media", "/mnt/media/Medien/Filme"), ("media", "/mnt/media/Medien/Serien"),
        ("disk", "/mnt/media", 95),
        ("smart", "/dev/disk/by-id/ata-WDC_WD20SPZX-22UA7T0_WD-WXF1EA83DVRS", "Mediendisk", 55),
    ]),
    "access-log": ("Zugriffs-Log (Proxy, Admin-Panel)", [
        ("unit", "access-log-proxy"), ("unit", "access-log-panel"),
        ("http", "http://127.0.0.1:8300/__access-log/health", {200}),
        ("http", "http://127.0.0.1:8310/health", {200}),
    ]),
    "homeboy": ("Homeboy (Pi, Tunnel)", [
        ("unit", "cloudflared"), ("unit", "claude-remote-control"),
        ("disk", "/", 90), ("temp", 80), ("mem", 8),
        ("smart", "/dev/disk/by-id/ata-Dogfish_SSD_256GB_KC20210317268", "System-SSD", 65),
    ]),
}


def check_unit(name):
    r = subprocess.run(["systemctl", "--user", "is-active", name], capture_output=True, text=True, timeout=10)
    state = r.stdout.strip() or "unknown"
    return None if state == "active" else f"Dienst {name} ist {state}"


def check_http(url, ok):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            code = response.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"{url} nicht erreichbar ({getattr(exc, 'reason', exc)})"
    good = code < 500 if ok == "<500" else code in ok
    return None if good else f"{url} antwortet mit {code}"


def check_tcp(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            return None
    except OSError as exc:
        return f"Port {port} nimmt keine Verbindung an ({exc})"


def check_disk(path, max_percent):
    usage = shutil.disk_usage(path)
    percent = usage.used * 100 / usage.total
    return None if percent < max_percent else f"Festplatte {path} zu {percent:.0f} % voll"


def check_temp(max_celsius):
    try:
        celsius = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        return None  # no sensor, nothing to judge
    return None if celsius < max_celsius else f"CPU-Temperatur {celsius:.0f} °C"


def check_mem(min_percent):
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        values[key] = int(rest.split()[0])
    percent = values["MemAvailable"] * 100 / values["MemTotal"]
    return None if percent >= min_percent else f"Nur noch {percent:.0f} % Arbeitsspeicher frei"


def check_media(path):
    """The external disk is mounted at boot with nofail, so it can simply be missing; a hung USB disk must not hang us."""
    try:
        r = subprocess.run(["ls", "-A", path], capture_output=True, text=True, timeout=8)
    except subprocess.TimeoutExpired:
        return f"{path} antwortet nicht (Platte hängt?)"
    if r.returncode != 0:
        return f"{path} nicht lesbar (Platte nicht eingehängt?)"
    return None if r.stdout.strip() else f"{path} ist leer (Platte nicht eingehängt?)"


def read_smart(device):
    """smartctl's JSON for the disk, or None while a sleeping hard disk is not to be woken up (-n standby)."""
    r = subprocess.run(["sudo", "-n", "smartctl", "-n", "standby", "-H", "-A", "-j", device],
                       capture_output=True, text=True, timeout=60)
    data = json.loads(r.stdout or "{}")
    if r.returncode & 3:  # bit 0/1: smartctl could not run or open the disk; exit 2 with power_mode standby = asleep
        if "standby" in json.dumps(data.get("power_mode", "")).lower() or "STANDBY" in r.stdout:
            return None
        raise RuntimeError(f"smartctl Fehler {r.returncode}")
    return data


def check_smart(device, label, max_celsius, now=None, reader=read_smart):
    """The disk's own health report (SMART). Read once an hour; the last result counts in between."""
    now = time.time() if now is None else now
    SMART_DIR.mkdir(exist_ok=True)
    path = SMART_DIR / (Path(device).name + ".json")
    st = load(path)
    if "checked" in st and now - st["checked"] < SMART_EVERY:
        return st.get("msg")
    try:
        data = reader(device)
    except Exception as exc:
        msg = f"{label}: SMART nicht lesbar ({exc})"
        path.write_text(json.dumps({**st, "checked": now, "msg": msg}))
        return msg
    if data is None:  # asleep: keep the last verdict, look again next minute
        return st.get("msg")
    msgs, baseline = [], st.get("baseline", {})
    if data.get("smart_status", {}).get("passed") is False:
        msgs.append(f"{label}: SMART meldet, dass die Platte ausfällt")
    for a in data.get("ata_smart_attributes", {}).get("table", []):
        raw, key = a.get("raw", {}).get("value", 0), str(a["id"])
        if a.get("thresh", 0) > 0 and a.get("value", 255) <= a["thresh"]:
            msgs.append(f"{label}: {a['name']} unter der Herstellergrenze ({a['value']} <= {a['thresh']})")
        if a["id"] in SMART_GROWING:
            if key in baseline and raw > baseline[key]:
                msgs.append(f"{label}: {SMART_GROWING[a['id']]} gestiegen von {baseline[key]} auf {raw}")
            baseline[key] = raw
    celsius = data.get("temperature", {}).get("current")
    if celsius is not None and celsius > max_celsius:
        msgs.append(f"{label}: {celsius} °C warm")
    msg = "; ".join(msgs) or None
    path.write_text(json.dumps({"checked": now, "msg": msg, "baseline": baseline}))
    return msg


CHECKS = {"smart": check_smart, "unit": check_unit, "http": check_http, "tcp": check_tcp, "disk": check_disk, "temp": check_temp, "mem": check_mem, "media": check_media}


def run_test(spec):
    """None when the test passes, otherwise a short German reason."""
    try:
        return CHECKS[spec[0]](*spec[1:])
    except Exception as exc:  # a broken test must show up as a failure, not as silence
        return f"Test {spec[0]} {spec[1:]} ist abgestürzt: {exc}"


def run_group(tests):
    return [msg for msg in (run_test(t) for t in tests) if msg]


def ping(url, body=None):
    """Healthchecks accepts GET and POST pings; retried because the point is to survive a bad moment."""
    data = body.encode()[:9000] if body else None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=10):
                return True
        except (urllib.error.URLError, OSError):
            pass
    return False


def load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def decide(failures, previous_fails):
    """-> (action, consecutive_failures); action is 'ok', 'fail' or 'wait'."""
    if not failures:
        return "ok", 0
    count = previous_fails + 1
    return ("fail" if count >= FAILS_BEFORE_ALERT else "wait"), count


def in_maintenance(key, now=None):
    flag = MAINT_DIR / key
    try:
        age = (now or time.time()) - flag.stat().st_mtime
    except OSError:
        return False
    return age < MAINT_MAX_HOURS * 3600


def set_maintenance(key, on):
    """on: pause the Healthchecks check (so the silence is not an alarm) and skip the group; off: the next success ping resumes it."""
    if key not in GROUPS:
        print(f"unbekannte Gruppe '{key}'", file=sys.stderr); return 1
    MAINT_DIR.mkdir(exist_ok=True)
    flag = MAINT_DIR / key
    if not on:
        flag.unlink(missing_ok=True)
        print(f"{key}: Wartung beendet, der naechste Lauf meldet wieder"); return 0
    flag.touch()
    url = load(CHECKS_FILE).get(key)
    api_key = next((l.split("=", 1)[1].strip() for l in (HERE / "healthchecks.env").read_text().splitlines() if l.startswith("HC_API_KEY=")), None)
    if url and api_key:
        uuid = url.rstrip("/").rsplit("/", 1)[-1]
        request = urllib.request.Request(f"https://healthchecks.io/api/v3/checks/{uuid}/pause", data=b"", method="POST",
                                         headers={"X-Api-Key": api_key, "User-Agent": USER_AGENT})
        try:
            urllib.request.urlopen(request, timeout=15).read()
            print(f"{key}: Wartung an, Check bei Healthchecks pausiert (laeuft nach {MAINT_MAX_HOURS} h von selbst ab)")
        except (urllib.error.URLError, OSError) as exc:
            print(f"{key}: Wartung an, aber Pausieren bei Healthchecks fehlgeschlagen ({exc}); es kann ein Alarm kommen")
    return 0


def run(dry=False):
    urls, state = load(CHECKS_FILE), load(STATE_FILE)
    active = [key for key in GROUPS if not in_maintenance(key)]
    with ThreadPoolExecutor(max_workers=len(GROUPS)) as pool:
        results = dict(zip(active, pool.map(lambda key: run_group(GROUPS[key][1]), active)))
    for key in GROUPS:
        if key not in results:
            state[key] = 0
            state[f"{key}_logged"] = False
            if dry:
                print(f"{GROUPS[key][0]}: Wartung, wird uebersprungen")
    for key, failures in results.items():
        name = GROUPS[key][0]
        action, state[key] = decide(failures, int(state.get(key, 0)))
        line = f"{name}: " + ("ok" if not failures else "; ".join(failures))
        if dry:
            print(line, f"[{action}]" if failures else "")
            continue
        if failures and not state.get(f"{key}_logged"):
            print(line, flush=True)  # goes to the journal; only changes are logged
        elif not failures and state.get(f"{key}_logged"):
            print(f"{name}: wieder ok", flush=True)
        state[f"{key}_logged"] = bool(failures)
        url = urls.get(key)
        if not url:
            continue
        if action == "ok":
            ping(url)
        elif action == "fail":
            ping(url + "/fail", line)
    if not dry:
        STATE_FILE.write_text(json.dumps(state))
    return results


def main(argv):
    if argv[:1] == ["ping"] and len(argv) == 2:
        url = load(CHECKS_FILE).get(argv[1])
        if not url:
            print(f"kein Check '{argv[1]}' eingerichtet", file=sys.stderr)
            return 0  # a missing monitor must never break the job that calls us
        return 0 if ping(url) else 1
    if argv[:1] == ["maintenance"] and len(argv) == 3 and argv[1] in ("on", "off"):
        return set_maintenance(argv[2], argv[1] == "on")
    run(dry="--dry-run" in argv)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
