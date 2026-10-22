#!/usr/bin/env python3
"""Creates (or updates) the Healthchecks.io checks and stores their ping URLs in checks.json.

Needs the project's *read-write API key* in ~/monitoring/healthchecks.env as  HC_API_KEY=...  (chmod 600).
Safe to run again: checks are matched by name, nothing is duplicated.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

from watchdog import CHECKS_FILE, GROUPS, HERE

API = "https://healthchecks.io/api/v3/checks/"
# Backups report once a day; the services report every minute and may be silent for 2 + 3 minutes.
BACKUPS = {"backup-buchnancials": "Backup Buchnancials", "backup-wagerl": "Backup Wagerl", "backup-secrets": "Backup Secrets"}


def api_key():
    for line in (HERE / "healthchecks.env").read_text().splitlines():
        if line.startswith("HC_API_KEY="):
            return line.split("=", 1)[1].strip()
    sys.exit("HC_API_KEY fehlt in healthchecks.env")


def upsert(key, payload):
    body = json.dumps({**payload, "unique": ["name"], "channels": "*", "tags": "homeboy"}).encode()
    request = urllib.request.Request(API, data=body, headers={"X-Api-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())["ping_url"]


def main():
    key = api_key()
    urls = {}
    for group, (name, _tests) in GROUPS.items():
        urls[group] = upsert(key, {"name": f"Homeboy: {name}", "timeout": 120, "grace": 180,
                                   "desc": "Prüft Dienst und öffentliche Adresse. Meldet sich jede Minute."})
    for group, name in BACKUPS.items():
        urls[group] = upsert(key, {"name": name, "timeout": 86400, "grace": 14400,
                                   "desc": "Meldet sich nach jedem erfolgreichen Backup (täglich)."})
    CHECKS_FILE.write_text(json.dumps(urls, indent=2))
    os.chmod(CHECKS_FILE, 0o600)
    print("angelegt:", ", ".join(urls))


if __name__ == "__main__":
    main()
