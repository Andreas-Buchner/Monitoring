# Homeboy monitoring

Alarm when an app on homeboy stops working, when the Pi itself dies, or when the home internet is gone.

How it works: `watchdog.py` runs every minute (`watchdog.timer`), tests each app (service running, local
health URL, public URL through the Cloudflare tunnel) plus disk, temperature and memory, and reports to
[Healthchecks.io](https://healthchecks.io). Healthchecks lives outside the Pi, so *silence* is an alarm too:
if the Pi, its power or the internet is down, the pings stop and Healthchecks sends the alert itself.

| Check            | Covers                                                                    |
|------------------|---------------------------------------------------------------------------|
| Wagerl           | service, `127.0.0.1:8220/health`, `https://wagerl.anmibu.com/health`      |
| Buchnancials     | service, `127.0.0.1:8200/health`, `https://buchnancials.anmibu.com/health`|
| Games            | service, `127.0.0.1:8210`, `https://games.anmibu.com`                     |
| Jellyfin         | `127.0.0.1:8096/health` (Docker container), media disk mounted and readable (`/mnt/media/Medien/Filme`, `/Serien`), disk < 95 %, SMART of the media disk (healthy, no growing error counters, < 55 °C) |
| MCP-Server       | gateway, site, e-paper, intervals.icu, system status (services + ports), `https://mcp.anmibu.com` |
| Homeboy          | cloudflared, claude-remote-control, disk < 90 %, CPU < 80 °C, RAM, SMART of the system SSD (< 65 °C) |
| Backup *         | the backup scripts (Buchnancials, Wagerl, encrypted secrets bundle) ping after every successful run (daily, 4 h grace) |

A test that fails once is ignored (a restart during a deploy); the second failure in a row sends
`<ping-url>/fail` with the reasons, which shows up in the alert. Alerts arrive about 2-5 minutes after an outage.

## Setup (once)
1. Sign up at healthchecks.io (free: 20 checks), create a project, copy its **API key (read-write)** from
   Project Settings, and put it into `~/monitoring/healthchecks.env` as `HC_API_KEY=...` (`chmod 600`).
2. `cd ~/monitoring && python3 setup_checks.py` creates the checks and writes the ping URLs to `checks.json`.
3. In Healthchecks, open Integrations and add where the alerts go (email is on by default; Telegram,
   ntfy or Pushover give a push on the phone).

## Operate
    python3 watchdog.py --dry-run          # run all tests now, ping nothing
    systemctl --user status watchdog.timer
    journalctl --user -u watchdog          # only state changes are logged
    python3 -m unittest discover -s tests

Add an app: add a group to `GROUPS` in `watchdog.py` and a line in `setup_checks.py` if it needs its own check,
then run `setup_checks.py` again (matched by name, no duplicates).
Backup scripts call `python3 ~/monitoring/watchdog.py ping backup-<name>`; without a check that is a no-op.

## SMART (disk health)
`smartctl` (package smartmontools) via `sudo -n`, once an hour per disk (`smart/<disk>.json` keeps the last result
and the error counters). Alarm when SMART says the disk fails, a value drops below the maker's threshold, a
counter of bad sectors / write / erase errors grows (one alarm per rise), or the disk is too warm. A sleeping
media disk is not woken up (`-n standby`).
