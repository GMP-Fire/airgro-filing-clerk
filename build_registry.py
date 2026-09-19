#!/usr/bin/env python3
"""
build_registry.py — ONE-OFF. Walks every folder the automations name, BY TITLE,
exactly once, and writes the ids to _AI Systems/claude-system/folder-registry.json.

Why
---
On 2026-09-07 the finance root was renamed and a title lookup created a decoy
folder within seven seconds. The root was then pinned by id, but every segment
below it still resolved by title, so renaming "4. Banking" would have done the
same thing one level down. After this runs, every consumer resolves a stable KEY
("fi.banking.fnb-gold-business-account") to an id, and a title becomes a label
Andrew is free to change.

This is the last title walk. It aborts if any path resolves to 0 folders or to
more than 1: a registry built on a guess is worse than no registry. It never
creates a folder and never overwrites an existing registry.

Usage (GitHub Actions: Build Folder Registry):
  python build_registry.py                    # print the registry, write nothing
  python build_registry.py --write            # also create folder-registry.json in Drive
  python build_registry.py --migrate-rules    # rewrite filing-rules.json dests as keys
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone

from googleapiclient.discovery import build

from filing_clerk import credentials, log

# The finance root, pinned by id since 2026-09-07. The one folder this walk starts from.
FINANCE_ROOT_ID = "12EAKPnQEl4KiPED6RjUwT_znkd-VXjPL"

FOLDER = "application/vnd.google-apps.folder"
REGISTRY_NAME = "folder-registry.json"
RULES_FOLDER = "_Filing Clerk"
RULES_NAME = "filing-rules.json"

# Folders that are not a rule's dest but that code already names. Key -> (anchor, path).
# anchor "fi" = the pinned finance root, "root" = My Drive.
FIXED = {
    "clerk.config": ("fi", "_Filing Clerk"),
    "fi.review.unfiled": ("fi", "_To review/Unfiled from Gmail"),
    "sys.inbox": ("root", "_Inbox"),
    "sys.claude-system": ("root", "_AI Systems/claude-system"),
    "sys.doc-index": ("root", "_AI Systems/claude-system/doc-index"),
}


def key_segment(title: str) -> str:
    """'4. Banking' -> 'banking'; 'FNB Cheque 62227042405' -> 'fnb-cheque-2405'.

    Long digit runs are account numbers: keep the last four, as a name would.
    """
    s = re.sub(r"^\d+\.\s*", "", title).lower()
    s = re.sub(r"\d{6,}", lambda m: m.group(0)[-4:], s)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def key_for(dest: str) -> str:
    return ".".join(["fi", *(key_segment(p) for p in dest.split("/") if p)])


class Walker:
    def __init__(self, svc):
        self.svc = svc
        self.problems: list[str] = []

    def child(self, parent: str, name: str, where: str) -> str | None:
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        q = f"'{parent}' in parents and name = '{safe}' and mimeType = '{FOLDER}' and trashed = false"
        files = self.svc.files().list(q=q, fields="files(id,name)", pageSize=10).execute().get("files", [])
        if len(files) != 1:
            ids = ", ".join(f["id"] for f in files) or "none"
            self.problems.append(f"{where}: {len(files)} folders named {name!r} ({ids})")
            return None
        return files[0]["id"]

    def walk(self, anchor_id: str, path: str, label: str) -> str | None:
        node = anchor_id
        for seg in [s for s in path.split("/") if s]:
            node = self.child(node, seg, f"{label}")
            if node is None:
                return None
        return node

    def find_file(self, parent: str, name: str) -> list[dict]:
        q = f"'{parent}' in parents and name = '{name}' and trashed = false"
        return self.svc.files().list(q=q, fields="files(id,name)", pageSize=10).execute().get("files", [])


def migrate_rules(svc, w: Walker, clerk: str, rules_id: str, rules: dict) -> int:
    """filing-rules.json: every dest path -> its registry key, in place (same file id,
    old content kept in Drive version history). Nothing but dest and the notes changes,
    and that is PROVEN below, not assumed."""
    reg_files = w.find_file(REGISTRY_PARENT_ID, REGISTRY_NAME)
    if len(reg_files) != 1:
        sys.exit(f"ABORTING: {len(reg_files)} {REGISTRY_NAME} in claude-system.")
    registry = json.loads(svc.files().get_media(fileId=reg_files[0]["id"]).execute().decode("utf-8"))["folders"]

    if all(r["dest"] in registry for r in rules["rules"]):
        log("filing-rules.json dests are already registry keys; nothing to do.")
        return 0
    new = json.loads(json.dumps(rules))
    for r in new["rules"]:
        k = r["dest"] if r["dest"] in registry else key_for(r["dest"])
        if k not in registry:
            sys.exit(f"ABORTING: rule {r['id']} dest {r['dest']!r} -> {k!r}, which is not in the registry.")
        r["dest"] = k
    prior = rules.get("version")
    new["version"] = "3.0"
    new["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new["note"] = rules["note"].replace(
        "Destinations are relative to 'My Drive/3. Financial Insurance'.",
        "Destinations are KEYS in _AI Systems/claude-system/folder-registry.json, which maps "
        "each key to a folder id - never a path. To add a destination, add a key there first.",
    )
    new["path_note"] = (
        "Since v3.0 (2026-09-19) every dest is a folder-registry key, resolved by id. Renaming "
        "any folder is safe: the clerk never walks titles and never creates a folder. A key "
        "whose folder is trashed or gone skips that rule with a Todoist item."
    )
    new["v3_0_note"] = (
        f"2026-09-19: every dest rewritten from a path to its folder-registry key by "
        f"build_registry.py --migrate-rules. NOTHING ELSE CHANGED from v{prior} - same rules in "
        "the same order, same never_file[], same folders (by id)."
    )

    # Proof: strip dest and the notes from both sides; the rest must be identical.
    def core(d):
        c = json.loads(json.dumps(d))
        for key in ("version", "updated", "note", "path_note", "v3_0_note"):
            c.pop(key, None)
        for r in c["rules"]:
            r.pop("dest")
        return c
    if core(new) != core(rules):
        sys.exit("ABORTING: the migration changed something other than dest. Nothing written.")
    for old, cur in zip(rules["rules"], new["rules"]):
        print(f"  {old['id']:42} {old['dest']}  ->  {cur['dest']}")

    from googleapiclient.http import MediaInMemoryUpload

    body = json.dumps(new, indent=1, ensure_ascii=False).encode()
    svc.files().update(fileId=rules_id, media_body=MediaInMemoryUpload(body, mimetype="application/json")).execute()
    log(f"filing-rules.json v{prior} -> v3.0 written in place ({rules_id}).")
    return 0


REGISTRY_PARENT_ID = "19K2DOo-bUw_ItGAFEAjVTTL-fVHXXpBi"  # sys.claude-system


def main() -> int:
    write = "--write" in sys.argv[1:]
    svc = build("drive", "v3", credentials=credentials(), cache_discovery=False)
    w = Walker(svc)

    root = svc.files().get(fileId=FINANCE_ROOT_ID, fields="id,name,mimeType,trashed").execute()
    if root.get("trashed") or root.get("mimeType") != FOLDER:
        sys.exit(f"ABORTING: finance root {FINANCE_ROOT_ID} is trashed or not a folder.")
    anchors = {"fi": FINANCE_ROOT_ID, "root": "root"}

    clerk = w.walk(FINANCE_ROOT_ID, RULES_FOLDER, "clerk.config")
    if not clerk:
        sys.exit("ABORTING: " + "; ".join(w.problems))
    rules_files = w.find_file(clerk, RULES_NAME)
    if len(rules_files) != 1:
        sys.exit(f"ABORTING: {len(rules_files)} {RULES_NAME} files in {RULES_FOLDER}.")
    rules = json.loads(svc.files().get_media(fileId=rules_files[0]["id"]).execute().decode("utf-8"))
    if "--migrate-rules" in sys.argv[1:]:
        return migrate_rules(svc, w, clerk, rules_files[0]["id"], rules)

    folders: dict[str, dict] = {"fi.root": {"id": FINANCE_ROOT_ID, "title_hint": root["name"]}}
    wanted: dict[str, tuple[str, str]] = dict(FIXED)
    for r in rules["rules"]:
        dest = r["dest"]
        k = key_for(dest)
        prior = wanted.get(k)
        if prior and prior != ("fi", dest):
            w.problems.append(f"key collision: {k} <- {prior[1]!r} and {dest!r}")
        wanted[k] = ("fi", dest)

    for k, (anchor, path) in sorted(wanted.items()):
        fid = w.walk(anchors[anchor], path, k)
        if fid:
            prefix = root["name"] + "/" if anchor == "fi" else ""
            folders[k] = {"id": fid, "title_hint": prefix + path}

    if w.problems:
        print("\n".join(["ABORTING - these paths did not resolve to exactly one folder:", *w.problems]))
        return 1

    registry = {
        "version": 1,
        "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": (
            "Folder ids by stable key. Consumers resolve KEY -> id with files.get and never "
            "walk titles; a title is a label Andrew may change. title_hint is what the path "
            "was called when this was built - for humans, never for lookup. Add a folder by "
            "adding a key here; never rename a key once something reads it."
        ),
        "folders": dict(sorted(folders.items())),
    }
    print(json.dumps(registry, indent=1, ensure_ascii=False))

    target = folders["sys.claude-system"]["id"]
    if w.find_file(target, REGISTRY_NAME):
        log(f"{REGISTRY_NAME} already exists in claude-system; not overwriting. Edit it in place.")
        return 0 if not write else 1
    if not write:
        log("Dry run: nothing written. Re-run with --write.")
        return 0

    from googleapiclient.http import MediaInMemoryUpload

    media = MediaInMemoryUpload(json.dumps(registry, indent=1, ensure_ascii=False).encode(), mimetype="application/json")
    created = svc.files().create(body={"name": REGISTRY_NAME, "parents": [target]}, media_body=media, fields="id").execute()
    log(f"WROTE {REGISTRY_NAME} id={created['id']} — pin this as REGISTRY_FILE_ID.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
