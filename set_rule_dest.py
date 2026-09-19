#!/usr/bin/env python3
"""
set_rule_dest.py — repoint ONE filing-rules.json rule at another folder-registry key.

The change is expressed as data and PROVEN: after the edit, everything except that
rule's dest, its note and the version fields must be byte-identical, or nothing is
written. The file is replaced in place (same id; Drive keeps the old version).

  python set_rule_dest.py RULE_ID NEW_KEY "why"            # show the change
  python set_rule_dest.py RULE_ID NEW_KEY "why" --apply    # write it
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

from filing_clerk import CLERK_KEY, RULES_NAME, Drive, credentials, log


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--apply"]
    if len(args) != 3:
        sys.exit(__doc__)
    rule_id, new_key, why = args
    drive = Drive(build("drive", "v3", credentials=credentials(), cache_discovery=False))
    drive.load_registry()
    if new_key not in drive.registry or not drive.folder(new_key):
        sys.exit(f"ABORTING: {new_key!r} is not a live folder-registry key.")
    rules_id = drive.find_config(drive.folder(CLERK_KEY), RULES_NAME)
    old = json.loads(drive.read_text(rules_id))
    new = json.loads(json.dumps(old))
    hits = [r for r in new["rules"] if r.get("id") == rule_id]
    if len(hits) != 1:
        sys.exit(f"ABORTING: {len(hits)} rules with id {rule_id!r}.")
    rule = hits[0]
    if rule["dest"] == new_key:
        log(f"{rule_id} already points at {new_key}; nothing to do.")
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log(f"{rule_id}: {rule['dest']} -> {new_key}")
    rule["note"] = (rule.get("note", "") + f" DEST {stamp}: {rule['dest']} -> {new_key}. {why}").strip()
    rule["dest"] = new_key
    major, minor = (str(old.get("version", "3.0")).split(".") + ["0"])[:2]
    new["version"] = f"{major}.{int(minor) + 1}"
    new["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def core(d):
        c = json.loads(json.dumps(d))
        for k in ("version", "updated"):
            c.pop(k, None)
        for r in c["rules"]:
            if r.get("id") == rule_id:
                r.pop("dest"); r.pop("note", None)
        return c
    if core(new) != core(old):
        sys.exit("ABORTING: the edit changed something beyond this rule's dest and note. Nothing written.")
    if "--apply" not in sys.argv:
        log("Dry run: nothing written. Re-run with --apply.")
        return 0
    body = json.dumps(new, indent=1, ensure_ascii=False).encode()
    drive.svc.files().update(fileId=rules_id, media_body=MediaInMemoryUpload(body, mimetype="application/json")).execute()
    log(f"filing-rules.json v{old.get('version')} -> v{new['version']} written in place.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
