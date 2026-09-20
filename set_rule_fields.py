#!/usr/bin/env python3
"""
set_rule_fields.py — change ONE filing-rules.json rule's name template and/or dedupe_on.

Sibling of set_rule_dest.py, same contract: the change is expressed as data and
PROVEN. After the edit, everything except that rule's name, dedupe_on, its note and
the version fields must be byte-identical, or nothing is written. A dest is NOT
reachable from here - repointing a folder is set_rule_dest.py's job and stays there.

  python set_rule_fields.py RULE_ID "why" --name "{yyyy-mm-next} X.pdf"
  python set_rule_fields.py RULE_ID "why" --dedupe-on size
  python set_rule_fields.py RULE_ID "why" --name "..." --dedupe-on size --apply

Without --apply it prints the change and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

from filing_clerk import CLERK_KEY, RULES_NAME, Drive, credentials, log


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("rule_id")
    ap.add_argument("why")
    ap.add_argument("--name", default=None)
    ap.add_argument("--dedupe-on", dest="dedupe_on", default=None)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if args.name is None and args.dedupe_on is None:
        sys.exit("Nothing to do: pass --name and/or --dedupe-on.")

    drive = Drive(build("drive", "v3", credentials=credentials(), cache_discovery=False))
    drive.load_registry()
    rules_id = drive.find_config(drive.folder(CLERK_KEY), RULES_NAME)
    old = json.loads(drive.read_text(rules_id))
    new = json.loads(json.dumps(old))

    hits = [r for r in new["rules"] if r.get("id") == args.rule_id]
    if len(hits) != 1:
        sys.exit(f"ABORTING: {len(hits)} rules with id {args.rule_id!r}.")
    rule = hits[0]

    changes = []
    if args.name is not None and rule.get("name") != args.name:
        changes.append(f"name: {rule.get('name')!r} -> {args.name!r}")
        rule["name"] = args.name
    if args.dedupe_on is not None and rule.get("dedupe_on", "") != args.dedupe_on:
        changes.append(f"dedupe_on: {rule.get('dedupe_on', '')!r} -> {args.dedupe_on!r}")
        rule["dedupe_on"] = args.dedupe_on
    if not changes:
        log(f"{args.rule_id} already has these values; nothing to do.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log(f"{args.rule_id}:")
    for c in changes:
        log(f"  {c}")
    rule["note"] = (rule.get("note", "") + f" EDIT {stamp}: " + "; ".join(changes) + f". {args.why}").strip()

    major, minor = (str(old.get("version", "3.0")).split(".") + ["0"])[:2]
    new["version"] = f"{major}.{int(minor) + 1}"
    new["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def core(d):
        c = json.loads(json.dumps(d))
        for k in ("version", "updated"):
            c.pop(k, None)
        for r in c["rules"]:
            if r.get("id") == args.rule_id:
                r.pop("name", None)
                r.pop("dedupe_on", None)
                r.pop("note", None)
        return c

    if core(new) != core(old):
        sys.exit("ABORTING: the edit changed something beyond this rule's name, dedupe_on and note. Nothing written.")
    log("Blast radius check passed.")

    if not args.apply:
        log("Dry run: nothing written. Re-run with --apply.")
        return 0
    body = json.dumps(new, indent=1, ensure_ascii=False).encode()
    drive.svc.files().update(
        fileId=rules_id, media_body=MediaInMemoryUpload(body, mimetype="application/json")
    ).execute()
    log(f"filing-rules.json v{old.get('version')} -> v{new['version']} written in place.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
