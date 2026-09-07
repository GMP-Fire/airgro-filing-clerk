#!/usr/bin/env python3
"""
Edit gmail-rules.json in Drive, safely, without the content passing through a chat window.

WHY THIS EXISTS
---------------
gmail-rules.json is ~35 KB. Changing it by hand means downloading it, editing, and
uploading; changing it through an assistant means the whole file being retyped, which is
how a stray comma or a truncated array destroys a config that four automations depend on.

This reads the file, makes one narrow structural change, writes a timestamped backup
alongside it, and writes it back. Nothing is retyped. The change is expressed as data.

WHAT IT CHANGES
---------------
Removes named senders from trash.from_any. That is the whole scope. It will not touch
protect, keep_inbox, reading, documents_of_record, archive or autofile, and there is a
check at the end that proves it did not.

    REMOVE_TRASH_SENDERS   comma-separated, matched EXACTLY against list entries
    NOTE                   one line appended to trash.from_any_note explaining why
    APPLY                  unset/0 = report the diff only. 1 = write.

Report-only by default, same as the sweep.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
RULES_NAME = "gmail-rules.json"

REMOVE = [s.strip().lower() for s in os.environ.get("REMOVE_TRASH_SENDERS", "").split(",") if s.strip()]
NOTE = os.environ.get("NOTE", "").strip()
APPLY = os.environ.get("APPLY", "").lower() in ("1", "true", "yes")

# Every list that must be byte-identical after the edit. If the change leaked anywhere
# else, this catches it before anything is written.
UNTOUCHED = ["protect", "keep_inbox", "reading", "documents_of_record",
             "archive", "autofile", "suspicious", "labels", "caps",
             "never_touch_queries", "unsubscribe"]


def log(m: str = "") -> None:
    print(m, flush=True)


def credentials() -> Credentials:
    missing = [k for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN_SWEEP")
               if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing secrets: {', '.join(missing)}")
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN_SWEEP"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds


def main() -> int:
    if not REMOVE:
        sys.exit("Nothing to do: set REMOVE_TRASH_SENDERS.")

    drive = build("drive", "v3", credentials=credentials(), cache_discovery=False)

    res = drive.files().list(
        q=f"name = '{RULES_NAME}' and trashed = false",
        fields="files(id,name,parents,modifiedTime)", pageSize=10).execute()
    files = res.get("files", [])
    if len(files) != 1:
        sys.exit(f"ABORTING: found {len(files)} files named {RULES_NAME}; exactly one must exist.")
    fid = files[0]["id"]
    parents = files[0].get("parents", [])

    raw = drive.files().get_media(fileId=fid).execute()
    rules = json.loads(raw.decode("utf-8"))
    before = copy.deepcopy(rules)

    current = rules.get("trash", {}).get("from_any", [])
    lower = {s.lower(): s for s in current}

    missing = [s for s in REMOVE if s not in lower]
    if missing:
        sys.exit("ABORTING - not present in trash.from_any, so nothing was changed:\n"
                 + "\n".join(f"  {m}" for m in missing)
                 + "\nCheck the exact spelling against the file.")

    keep = [s for s in current if s.lower() not in REMOVE]
    removed = [lower[s] for s in REMOVE]
    rules["trash"]["from_any"] = keep

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if NOTE:
        rules["trash"]["from_any_note"] = (
            rules["trash"].get("from_any_note", "") + f" REMOVED {stamp}: "
            + ", ".join(removed) + ". " + NOTE)

    log(f"{RULES_NAME}  v{rules.get('version')}  ({fid})")
    log(f"trash.from_any: {len(current)} -> {len(keep)}\n")
    log("REMOVING:")
    for r in removed:
        log(f"  - {r}")
    log()

    # Prove the blast radius. Anything outside trash.from_any must be untouched.
    drift = [k for k in UNTOUCHED if before.get(k) != rules.get(k)]
    if drift:
        sys.exit(f"ABORTING - the edit changed sections it must not: {drift}")
    other_trash = {k: v for k, v in rules.get("trash", {}).items()
                   if k not in ("from_any", "from_any_note")}
    if other_trash != {k: v for k, v in before.get("trash", {}).items()
                       if k not in ("from_any", "from_any_note")}:
        sys.exit("ABORTING - the edit changed something else inside trash{}.")
    log(f"Blast radius check passed: only trash.from_any"
        f"{' and its note' if NOTE else ''} changed.\n")

    if not APPLY:
        log("REPORT ONLY. Nothing was written. Re-run with APPLY=1.")
        return 0

    # Backup first, in the same folder, before the original is touched.
    #
    # THE NAME MATTERS. The first version called it "gmail-rules.backup-<stamp>.json",
    # which put a second file containing the string "gmail-rules" next to the real one.
    # This code matches filenames exactly so it was unaffected - but the Gmail Steward is a
    # language model using the Drive connector, and a title-contains search would return
    # both and could read a stale backup as the live rule table. That is the same
    # duplicate-config hazard the Filing Clerk aborts on, introduced by the safety net.
    #
    # So backups carry a name that shares no searchable substring with the file they
    # protect, and say plainly in the name what they are.
    body = json.dumps(before, indent=2, ensure_ascii=False).encode("utf-8")
    backup = drive.files().create(
        body={"name": f"ARCHIVED-rule-table-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
                      f"-DO-NOT-READ.json",
              "parents": parents},
        media_body=MediaInMemoryUpload(body, mimetype="application/json"),
        fields="id,name").execute()
    log(f"Backup written: {backup['name']}  ({backup['id']})")

    payload = json.dumps(rules, indent=2, ensure_ascii=False).encode("utf-8")
    drive.files().update(
        fileId=fid,
        media_body=MediaInMemoryUpload(payload, mimetype="application/json")).execute()
    log(f"Updated {RULES_NAME}: {len(keep)} senders now on trash.from_any.")
    log("The Steward reads this file on every run, so it takes effect immediately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
