#!/usr/bin/env python3
"""
registry_edit.py — edit folder-registry.json in place, by id, with proof.

  python registry_edit.py --drop KEY [KEY ...]      # remove keys nothing reads any more
  python registry_edit.py --set KEY=FOLDER_ID ...   # add or repoint a key
  python registry_edit.py --refresh-hints           # title_hint := the folder's LIVE title
  ...add --apply to write. Without it, nothing is written.

Rules the file lives by, enforced here:
  - a key must resolve to a live folder, or it is refused (except --drop, whose whole
    point is a key that no longer does);
  - a key in use by a filing-rules.json dest may not be dropped;
  - only the keys named on the command line may change, and that is checked before
    anything is written.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload

from filing_clerk import CLERK_KEY, FOLDER_MIME, REGISTRY_FILE_ID, RULES_NAME, Drive, credentials, log


def main() -> int:
    args = sys.argv[1:]
    apply = "--apply" in args
    drops = [a for a in args[args.index("--drop") + 1:] if not a.startswith("--")] if "--drop" in args else []
    sets = dict(a.split("=", 1) for a in args if "=" in a and not a.startswith("--"))
    hints = "--refresh-hints" in args
    if not (drops or sets or hints):
        sys.exit(__doc__)

    svc = build("drive", "v3", credentials=credentials(), cache_discovery=False)
    drive = Drive(svc)
    drive.load_registry()
    old = json.loads(drive.read_text(REGISTRY_FILE_ID))
    new = json.loads(json.dumps(old))
    folders = new["folders"]

    in_use = set()
    clerk = drive.folder(CLERK_KEY)
    if clerk:
        rules = json.loads(drive.read_text(drive.find_config(clerk, RULES_NAME)))
        in_use = {r["dest"] for r in rules["rules"]}

    for key in drops:
        if key not in folders:
            log(f"{key}: not in the registry, nothing to drop")
            continue
        if key in in_use:
            sys.exit(f"ABORTING: {key} is the dest of a filing rule. Repoint the rule first.")
        log(f"drop {key} -> {folders[key]['id']} ({folders[key].get('title_hint')})")
        folders.pop(key)

    for key, fid in sets.items():
        meta = svc.files().get(fileId=fid, fields="id,name,mimeType,trashed").execute()
        if meta.get("trashed") or meta["mimeType"] != FOLDER_MIME:
            sys.exit(f"ABORTING: {fid} is trashed or not a folder.")
        log(f"set {key} -> {fid} ({meta['name']}){' [replaces ' + folders[key]['id'] + ']' if key in folders else ''}")
        folders[key] = {"id": fid, "title_hint": meta["name"]}

    if hints:
        for key, entry in folders.items():
            try:
                meta = svc.files().get(fileId=entry["id"], fields="name,trashed,mimeType").execute()
            except HttpError as exc:
                log(f"HINT SKIP {key}: {exc}")
                continue
            if meta.get("trashed") or meta["mimeType"] != FOLDER_MIME:
                log(f"HINT SKIP {key}: trashed or not a folder — drop it or repoint it")
                continue
            if entry.get("title_hint") != meta["name"]:
                log(f"hint {key}: {entry.get('title_hint')!r} -> {meta['name']!r}")
                entry["title_hint"] = meta["name"]

    touched = {k for k in set(old["folders"]) | set(folders)
               if old["folders"].get(k) != folders.get(k)}
    unexpected = touched - set(drops) - set(sets) - (touched if hints else set())
    if unexpected:
        sys.exit(f"ABORTING: keys changed that no argument named: {', '.join(sorted(unexpected))}")

    new["version"] = int(old.get("version", 1)) + 1
    new["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log(f"{len(old['folders'])} keys -> {len(folders)}; registry v{old.get('version')} -> v{new['version']}")
    if not apply:
        log("Dry run: nothing written. Re-run with --apply.")
        return 0
    body = json.dumps(new, indent=1, ensure_ascii=False).encode()
    svc.files().update(fileId=REGISTRY_FILE_ID, media_body=MediaInMemoryUpload(body, mimetype="application/json")).execute()
    log("folder-registry.json written in place.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
