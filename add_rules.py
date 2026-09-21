#!/usr/bin/env python3
"""
add_rules.py - APPEND new rules to filing-rules.json from a JSON file in this repo.

Sibling of set_rule_fields.py, same contract: the change is data, and it is PROVEN.
After the edit, the existing rules and every other key must be byte-identical and
the only difference is the appended rules plus the version fields - or nothing is
written. It never edits or removes an existing rule; an id that already exists
aborts the run. Every new rule's dest must resolve in the folder registry, so a
typo is caught here, not as a Todoist item at 03:10.

  python add_rules.py rule-additions/2026-09-21-school-fees.json          # report
  python add_rules.py rule-additions/2026-09-21-school-fees.json --apply  # write
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

from filing_clerk import CLERK_KEY, RULES_NAME, Drive, Rule, credentials, log


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("path")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    with open(args.path, encoding="utf-8") as fh:
        additions = json.load(fh)
    if not isinstance(additions, list) or not additions:
        sys.exit(f"{args.path} must hold a non-empty JSON list of rules.")
    for raw in additions:
        for k in ("id", "dest", "name"):
            if not raw.get(k):
                sys.exit(f"ABORTING: a rule is missing {k!r}: {raw}")
        Rule.parse(raw)  # same parser the clerk uses; a bad shape fails here
        if raw.get("attachment_re"):
            re.compile(raw["attachment_re"])

    drive = Drive(build("drive", "v3", credentials=credentials(), cache_discovery=False))
    drive.load_registry()
    rules_id = drive.find_config(drive.folder(CLERK_KEY), RULES_NAME)
    old = json.loads(drive.read_text(rules_id))
    new = json.loads(json.dumps(old))

    have = {r.get("id") for r in old["rules"]}
    ids = [r["id"] for r in additions]
    clash = sorted(set(ids) & have) + sorted({i for i in ids if ids.count(i) > 1})
    if clash:
        sys.exit(f"ABORTING: rule id(s) already exist or repeat: {clash}. Nothing written.")
    dead = [r["dest"] for r in additions if not drive.folder(r["dest"])]
    if dead:
        sys.exit(f"ABORTING: dest key(s) do not resolve to a live folder: {sorted(set(dead))}. Nothing written.")
    for raw in additions:
        log(f"+ {raw['id']} -> {raw['dest']}  name={raw['name']!r}")

    new["rules"].extend(additions)
    major, minor = (str(old.get("version", "3.0")).split(".") + ["0"])[:2]
    new["version"] = f"{major}.{int(minor) + 1}"
    new["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def core(d):
        c = json.loads(json.dumps(d))
        for k in ("version", "updated"):
            c.pop(k, None)
        c["rules"] = c["rules"][: len(old["rules"])]
        return c

    if core(new) != core(old) or new["rules"][len(old["rules"]):] != additions:
        sys.exit("ABORTING: the edit changed something beyond appending these rules. Nothing written.")
    log("Blast radius check passed.")

    if not args.apply:
        log("Dry run: nothing written. Re-run with --apply.")
        return 0
    body = json.dumps(new, indent=1, ensure_ascii=False).encode()
    drive.svc.files().update(
        fileId=rules_id, media_body=MediaInMemoryUpload(body, mimetype="application/json")
    ).execute()
    log(f"filing-rules.json v{old.get('version')} -> v{new['version']} written in place ({len(additions)} rules added).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
