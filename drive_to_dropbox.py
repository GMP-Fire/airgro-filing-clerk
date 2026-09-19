#!/usr/bin/env python3
"""
drive_to_dropbox.py — ONE-OFF (Drive v2 Phase 2.2). Copies a Drive folder tree into
the Airgro Dropbox, byte for byte, and PROVES each file landed.

It COPIES. It never trashes, moves or edits anything in Drive: removing the Drive
original is a separate step, taken only after the manifest says every file verified.

Proof per file: Dropbox returns a content_hash for what it stored; this computes
the same hash over the bytes it downloaded from Drive, and a mismatch is a FAIL.
Re-runnable: a file already in Dropbox with the same hash is skipped, so a run cut
short by the token expiring (a console token lasts 4 hours) just runs again.

Google Docs/Sheets/Slides have no bytes of their own and are exported to
docx/xlsx/pptx. Anything else native (Forms, shortcuts, maps) cannot be exported
and is listed as SKIPPED in the manifest, never silently dropped.

  python drive_to_dropbox.py            # walk and plan; touches nothing
  python drive_to_dropbox.py --copy     # copy, verify, write the manifest
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sys
import time

import requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from filing_clerk import credentials, log

SOURCE_ID = os.environ.get("SOURCE_ID", "14b3QTXFoywV09Y9NuqHAfWsBl6Cg_mDb")  # 08. Career/01. Airgro
TARGET = os.environ.get("TARGET", "/Andrew Wills/Drive migration 2026-09/01. Airgro")
TEAM_ROOT_NS = os.environ.get("DROPBOX_ROOT_NS", "1963053987")  # Airgro team space root

FOLDER = "application/vnd.google-apps.folder"
EXPORT = {
    "application/vnd.google-apps.document": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}
BLOCK = 4 * 1024 * 1024
SIMPLE_MAX = 140 * 1024 * 1024
TIMEOUT = (15, 300)  # connect, read — every request is bounded


def content_hash(data: bytes) -> str:
    """Dropbox's content_hash: SHA-256 of the concatenated SHA-256s of 4 MB blocks."""
    return hashlib.sha256(
        b"".join(hashlib.sha256(data[i:i + BLOCK]).digest() for i in range(0, len(data), BLOCK))
    ).hexdigest()


def clean(name: str) -> str:
    """A Drive title as a Dropbox path segment: no slash, no trailing space or dot."""
    return name.replace("/", "-").replace("\\", "-").rstrip(" .") or "_"


# ------------------------------------------------------------------------ drive


def walk(svc, folder_id: str, rel: str, out: list[dict]) -> None:
    page = None
    seen: dict[str, int] = {}
    while True:
        resp = svc.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id,name,mimeType,size)",
            pageSize=1000, pageToken=page,
        ).execute()
        for f in sorted(resp.get("files", []), key=lambda x: (x["mimeType"] != FOLDER, x["name"])):
            name = clean(f["name"])
            if f["mimeType"] in EXPORT and not name.lower().endswith(EXPORT[f["mimeType"]][1]):
                name += EXPORT[f["mimeType"]][1]
            # Drive allows two items with one name in a folder; Dropbox does not.
            k = name.lower()
            seen[k] = seen.get(k, 0) + 1
            if seen[k] > 1:
                stem, dot, ext = name.rpartition(".") if f["mimeType"] != FOLDER else (name, "", "")
                name = f"{stem or name} (drive {f['id'][:6]}){dot}{ext}" if stem else f"{name} (drive {f['id'][:6]})"
            path = f"{rel}/{name}"
            if f["mimeType"] == FOLDER:
                walk(svc, f["id"], path, out)
            else:
                out.append({**f, "rel": path})
        page = resp.get("nextPageToken")
        if not page:
            return


def download(svc, f: dict) -> bytes:
    if f["mimeType"] in EXPORT:
        return svc.files().export(fileId=f["id"], mimeType=EXPORT[f["mimeType"]][0]).execute()
    return svc.files().get_media(fileId=f["id"]).execute()


# ---------------------------------------------------------------------- dropbox


class Dropbox:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self.root = json.dumps({".tag": "root", "root": TEAM_ROOT_NS})

    def _call(self, url: str, *, arg: dict | None = None, body: bytes | None = None, js: dict | None = None):
        headers = {"Dropbox-API-Path-Root": self.root}
        if arg is not None:
            headers["Dropbox-API-Arg"] = json.dumps(arg, ensure_ascii=True)
            headers["Content-Type"] = "application/octet-stream"
        for attempt in range(6):
            r = self.s.post(url, headers=headers, data=body, json=js, timeout=TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("Retry-After", 2 ** attempt))
                log(f"[dropbox] {r.status_code}, waiting {wait:.0f}s")
                time.sleep(wait)
                continue
            return r
        return r

    def existing_hash(self, path: str) -> str | None:
        r = self._call("https://api.dropboxapi.com/2/files/get_metadata", js={"path": path})
        if r.status_code == 409:
            return None
        r.raise_for_status()
        return r.json().get("content_hash")

    def upload(self, path: str, data: bytes) -> dict:
        commit = {"path": path, "mode": "add", "autorename": False, "mute": True}
        if len(data) <= SIMPLE_MAX:
            r = self._call("https://content.dropboxapi.com/2/files/upload", arg=commit, body=data)
        else:
            chunk = 64 * 1024 * 1024
            r = self._call("https://content.dropboxapi.com/2/files/upload_session/start", arg={}, body=data[:chunk])
            r.raise_for_status()
            sid, off = r.json()["session_id"], chunk
            while len(data) - off > chunk:
                r = self._call("https://content.dropboxapi.com/2/files/upload_session/append_v2",
                               arg={"cursor": {"session_id": sid, "offset": off}}, body=data[off:off + chunk])
                r.raise_for_status()
                off += chunk
            r = self._call("https://content.dropboxapi.com/2/files/upload_session/finish",
                           arg={"cursor": {"session_id": sid, "offset": off}, "commit": commit}, body=data[off:])
        if r.status_code != 200:
            raise RuntimeError(f"{r.status_code} {r.text[:300]}")
        return r.json()


# -------------------------------------------------------------------------- run


def main() -> int:
    copy = "--copy" in sys.argv[1:]
    svc = build("drive", "v3", credentials=credentials(), cache_discovery=False)
    src = svc.files().get(fileId=SOURCE_ID, fields="name,mimeType,trashed").execute()
    if src["mimeType"] != FOLDER or src.get("trashed"):
        sys.exit(f"ABORTING: source {SOURCE_ID} is not a live folder.")
    files: list[dict] = []
    walk(svc, SOURCE_ID, "", files)
    native_skip = [f for f in files if f["mimeType"].startswith("application/vnd.google-apps.") and f["mimeType"] not in EXPORT]
    total = sum(int(f.get("size") or 0) for f in files)
    log(f"Source {src['name']!r}: {len(files)} files, {total / 1e9:.2f} GB (+{sum(f['mimeType'] in EXPORT for f in files)} to export, {len(native_skip)} unexportable)")
    log(f"Target: {TARGET}")
    if not copy:
        for f in files[:15]:
            log(f"  plan: {f['rel']}")
        log("Plan only: nothing copied. Re-run with --copy.")
        return 0

    token = os.environ.get("DROPBOX_TOKEN")
    if not token:
        sys.exit("ABORTING: DROPBOX_TOKEN secret is not set.")
    dbx = Dropbox(token)
    rows, fails = [], 0
    for i, f in enumerate(files, 1):
        dest = TARGET + f["rel"]
        row = {"drive_id": f["id"], "path": f["rel"], "size": f.get("size", ""), "status": "", "detail": ""}
        try:
            if f in native_skip:
                row["status"] = "SKIPPED"
                row["detail"] = f"unexportable {f['mimeType']}"
            else:
                data = download(svc, f)
                h = content_hash(data)
                row["content_hash"] = h
                if dbx.existing_hash(dest) == h:
                    row["status"] = "ALREADY"
                else:
                    got = dbx.upload(dest, data).get("content_hash")
                    row["status"] = "VERIFIED" if got == h else "FAIL"
                    row["detail"] = "" if got == h else f"hash mismatch: dropbox {got}"
        except (HttpError, RuntimeError, requests.RequestException) as exc:
            row["status"], row["detail"] = "FAIL", str(exc)[:300]
        fails += row["status"] == "FAIL"
        rows.append(row)
        if row["status"] == "FAIL" or i % 100 == 0:
            log(f"[{i}/{len(files)}] {row['status']} {f['rel']} {row['detail']}")

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["status", "path", "size", "drive_id", "content_hash", "detail"])
    w.writeheader()
    w.writerows(rows)
    with open("migration-manifest.csv", "w") as fh:
        fh.write(buf.getvalue())
    dbx.upload(f"{TARGET}/00-MIGRATION-MANIFEST {time.strftime('%Y%m%dT%H%M%S')}.csv", buf.getvalue().encode())
    counts = {s: sum(r["status"] == s for r in rows) for s in ("VERIFIED", "ALREADY", "SKIPPED", "FAIL")}
    log(f"Done: {counts}. Drive was not touched.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
