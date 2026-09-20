#!/usr/bin/env python3
"""
rename_folders.py — apply an approved folder-rename plan, BY ID.

Input: data-folder-renames.csv (folder_id, current_path, current_name, proposed_name, issue),
generated from the Drive inventory and approved by Andrew on 2026-09-20.

A rename only ever changes a TITLE. Ids never change, so nothing that resolves a folder
through folder-registry.json can break, and the order of the rows does not matter.

Three refusals, because a plan is a snapshot and Drive is live:
  - the id does not resolve, is trashed, or is not a folder  -> skipped, reported
  - its live name is not the plan's current_name             -> skipped, reported
    (somebody renamed it since the plan was made; the plan is stale for that row)
  - a sibling already holds the proposed name                -> skipped, reported

  python rename_folders.py            # show what would change
  python rename_folders.py --apply    # rename
"""

from __future__ import annotations

import csv
import sys

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from filing_clerk import FOLDER_MIME, credentials, log

PLAN = "data-folder-renames.csv"


def main() -> int:
    apply = "--apply" in sys.argv[1:]
    svc = build("drive", "v3", credentials=credentials(), cache_discovery=False)
    with open(PLAN, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    log(f"{len(rows)} planned rename(s) from {PLAN}")

    done = skipped = 0
    for row in rows:
        fid, want_old, want_new = row["folder_id"], row["current_name"], row["proposed_name"]
        try:
            meta = svc.files().get(fileId=fid, fields="id,name,mimeType,trashed,parents").execute()
        except HttpError as exc:
            log(f"SKIP {want_old!r}: id {fid} does not resolve ({exc})")
            skipped += 1
            continue
        if meta.get("trashed") or meta.get("mimeType") != FOLDER_MIME:
            log(f"SKIP {want_old!r}: trashed or not a folder")
            skipped += 1
            continue
        if meta["name"] == want_new:
            continue  # already applied - a re-run is a no-op
        if meta["name"] != want_old:
            log(f"SKIP {fid}: live name is {meta['name']!r}, plan expected {want_old!r} — plan is stale for this row")
            skipped += 1
            continue
        parent = (meta.get("parents") or [None])[0]
        if parent:
            safe = want_new.replace("\\", "\\\\").replace("'", "\\'")
            clash = svc.files().list(
                q=f"'{parent}' in parents and name = '{safe}' and mimeType = '{FOLDER_MIME}' and trashed = false",
                fields="files(id)", pageSize=2).execute().get("files", [])
            if clash:
                log(f"SKIP {want_old!r}: a sibling is already called {want_new!r}")
                skipped += 1
                continue
        if not apply:
            log(f"would rename {want_old!r} -> {want_new!r}   ({row['current_path']})")
            done += 1
            continue
        svc.files().update(fileId=fid, body={"name": want_new}).execute()
        log(f"renamed {want_old!r} -> {want_new!r}")
        done += 1

    log(f"{'Renamed' if apply else 'Would rename'} {done}, skipped {skipped}." + ("" if apply else " Re-run with --apply."))
    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
