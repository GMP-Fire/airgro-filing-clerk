#!/usr/bin/env python3
"""
apply_ops.py — apply a reviewed plan of Drive operations, BY ID.

Input: data-ops.csv, columns op,target,parent,name
  mkdir  target=@label (a name for the new folder, used later in this file)
         parent=<folder id or @label>   name=<folder name>
  move   target=<file/folder id>        parent=<folder id or @label>   name=<optional new name>
  empty  target=<folder id>              — trash every child of that folder, the folder itself kept
  trash  target=<file/folder id>         — trash it; a FOLDER is refused unless it is empty

@labels are bound as the run goes, so a file can be moved into a folder this same
plan creates. Everything is addressed by id: no title walk, no path, no folder
created except where the plan says mkdir. A mkdir whose name already exists binds
the existing folder instead of making a second one.

  python apply_ops.py            # show what would happen
  python apply_ops.py --apply
"""

from __future__ import annotations

import csv
import sys

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from filing_clerk import FOLDER_MIME, credentials, log

PLAN = "data-ops.csv"


def main() -> int:
    apply = "--apply" in sys.argv[1:]
    svc = build("drive", "v3", credentials=credentials(), cache_discovery=False)
    with open(PLAN, newline="", encoding="utf-8") as fh:
        ops = list(csv.DictReader(fh))
    labels: dict[str, str] = {}
    done = failed = 0

    def resolve(ref: str) -> str | None:
        return labels.get(ref) if ref.startswith("@") else ref

    for op in ops:
        kind, target, parent_ref, name = op["op"], op["target"], op["parent"], op.get("name", "")

        if kind in ("empty", "trash"):
            try:
                f = svc.files().get(fileId=target, fields="id,name,mimeType,trashed").execute()
            except HttpError as exc:
                log(f"FAIL {kind} {target}: {exc}")
                failed += 1
                continue
            if f.get("trashed"):
                log(f"skip {kind} {f['name']}: already in the trash")
                continue
            children = []
            if f["mimeType"] == FOLDER_MIME:
                children = svc.files().list(q=f"'{target}' in parents and trashed = false",
                                            fields="files(id,name)", pageSize=1000).execute().get("files", [])
            if kind == "trash":
                if children:
                    # Trashing a folder takes everything in it with it. A plan that says
                    # "trash this empty shell" must not quietly bin 40 files.
                    log(f"REFUSED trash {f['name']}: holds {len(children)} item(s); empty it first")
                    failed += 1
                    continue
                if not apply:
                    log(f"would trash {f['name']}")
                    done += 1
                    continue
                svc.files().update(fileId=target, body={"trashed": True}).execute()
                log(f"trashed {f['name']}")
                done += 1
                continue
            for c in children:
                if not apply:
                    log(f"would trash {f['name']}/{c['name']}")
                    done += 1
                    continue
                svc.files().update(fileId=c["id"], body={"trashed": True}).execute()
                log(f"trashed {f['name']}/{c['name']}")
                done += 1
            if not children:
                log(f"{f['name']} is already empty")
            continue

        parent = resolve(parent_ref)
        if parent is None:
            log(f"FAIL {kind} {name or target}: label {parent_ref} was never bound")
            failed += 1
            continue
        try:
            meta = svc.files().get(fileId=parent, fields="id,name,mimeType,trashed").execute()
            if meta.get("trashed") or meta["mimeType"] != FOLDER_MIME:
                raise HttpError(None, b"parent is trashed or not a folder")  # type: ignore[arg-type]
        except HttpError as exc:
            log(f"FAIL {kind} {name or target}: parent {parent} does not resolve ({exc})")
            failed += 1
            continue

        if kind == "mkdir":
            safe = name.replace("\\", "\\\\").replace("'", "\\'")
            found = svc.files().list(
                q=f"'{parent}' in parents and name = '{safe}' and mimeType = '{FOLDER_MIME}' and trashed = false",
                fields="files(id)", pageSize=2).execute().get("files", [])
            if found:
                labels[target] = found[0]["id"]
                log(f"exists  {meta['name']}/{name} -> {found[0]['id']} (bound {target}, nothing created)")
                continue
            if not apply:
                labels[target] = f"<new {name}>"
                log(f"would create {meta['name']}/{name}")
                done += 1
                continue
            made = svc.files().create(body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}, fields="id").execute()
            labels[target] = made["id"]
            log(f"created {meta['name']}/{name} -> {made['id']} (bound {target})")
            done += 1

        elif kind == "move":
            try:
                f = svc.files().get(fileId=target, fields="id,name,parents,trashed").execute()
            except HttpError as exc:
                log(f"FAIL move {target}: {exc}")
                failed += 1
                continue
            if f.get("trashed"):
                log(f"SKIP move {f['name']}: in the trash")
                continue
            if not apply:
                log(f"would move {f['name']} -> {meta['name']}/{name or f['name']}")
                done += 1
                continue
            body = {"name": name} if name else {}
            svc.files().update(fileId=target, body=body, addParents=parent,
                               removeParents=",".join(f.get("parents", [])), fields="id").execute()
            log(f"moved {f['name']} -> {meta['name']}/{name or f['name']}")
            done += 1
        else:
            log(f"FAIL unknown op {kind!r}")
            failed += 1

    log(f"{'Applied' if apply else 'Would apply'} {done}, failed {failed}." + ("" if apply else " Re-run with --apply."))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
