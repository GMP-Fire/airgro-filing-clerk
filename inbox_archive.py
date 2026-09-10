#!/usr/bin/env python3
"""
Inbox Archive — take the old, read, already-handled mail out of the inbox.

WHY THIS EXISTS, AND WHY THE BACKLOG SWEEP IS NOT IT
----------------------------------------------------
On 2026-09-10 the inbox held 25,653 threads and 17 unread. A count-only run of
backlog_sweep.py against it found ONE sweepable message across the five largest
senders; everything else was on its held-back list — Spotify receipts, Torch SA
order confirmations, Hirschs and Jacana orders, news24 wraps.

That is the sweep working correctly, not failing. It targets a blacklist of ~82
marketing senders and then refuses anything whose subject looks like a receipt or
an order. The marketing is already gone: Trash held 8,593 threads from earlier
passes. What remains in the inbox is ordinary correspondence and records going
back to 2019, which no sender blacklist should ever touch.

Those messages do not need deleting. They need to stop being in the inbox.

WHAT THIS DOES
--------------
Removes the INBOX label from old mail. That is all. It never trashes, never
deletes, and never marks anything read. Archived mail stays in All Mail and stays
searchable forever; putting a message back is one click.

THE SAFETY PROPERTY THAT MATTERS
--------------------------------
It only ever touches mail that is ALREADY READ (-is:unread in the query). Anything
unread is invisible to this tool by construction, so it can never make something
needing Andrew's attention disappear. Starred mail and the '*Action this Day'
label are protected on top of that.

Because every exclusion lives in the Gmail query or in a label lookup, this reads
no message bodies at all — it costs a few hundred quota units for the whole
mailbox, where the sweep spent thousands.
"""

from __future__ import annotations

import datetime as dt
import os
import sys

from googleapiclient.discovery import build

from backlog_sweep import call, credentials, log

ARCHIVE = os.environ.get("ARCHIVE", "").lower() in ("1", "true", "yes")
MAX_ARCHIVE = int(os.environ.get("MAX_ARCHIVE", "0"))       # 0 = no limit
BATCH = 1000                                                # batchModify ids per call

# Default cut-off: anything older than 12 months. Deliberately conservative — the
# recent window is where a thread might still be live.
_DEFAULT_BEFORE = (dt.date.today() - dt.timedelta(days=365)).strftime("%Y/%m/%d")
BEFORE = os.environ.get("BEFORE", "").strip() or _DEFAULT_BEFORE

# Labels whose mail stays in the inbox whatever its age. Names, not ids — ids are
# resolved at run time, and a name that no longer exists is reported, not guessed.
PROTECTED_LABELS = ["*Action this Day", "Steward/Suspicious"]

BASE_QUERY = "in:inbox -is:starred -is:unread"


def resolve_protected(gmail) -> tuple[set[str], list[str]]:
    """Message ids under a protected label, and the names that did not resolve."""
    existing = {
        lb["name"]: lb["id"]
        for lb in call(gmail.users().labels().list(userId="me"), units=1).get("labels", [])
    }
    ids: set[str] = set()
    missing: list[str] = []
    for name in PROTECTED_LABELS:
        label_id = existing.get(name)
        if not label_id:
            missing.append(name)
            continue
        page = None
        while True:
            resp = call(
                gmail.users().messages().list(
                    userId="me", labelIds=[label_id], maxResults=500, pageToken=page
                )
            )
            ids.update(m["id"] for m in resp.get("messages", []))
            page = resp.get("nextPageToken")
            if not page:
                break
        log(f"protected: {name} ({len(ids)} ids so far)")
    return ids, missing


def list_ids(gmail, query: str) -> list[str]:
    ids: list[str] = []
    page = None
    while True:
        resp = call(
            gmail.users().messages().list(
                userId="me", q=query, maxResults=500, pageToken=page
            )
        )
        ids.extend(m["id"] for m in resp.get("messages", []))
        page = resp.get("nextPageToken")
        if not page:
            break
    return ids


def year_buckets(before: str) -> list[tuple[str, str, str]]:
    """(label, after, before) windows from 2015 up to the cut-off, oldest first."""
    cut = dt.datetime.strptime(before, "%Y/%m/%d").date()
    out = []
    for year in range(2015, cut.year + 1):
        start = dt.date(year, 1, 1)
        end = min(dt.date(year + 1, 1, 1), cut)
        if end <= start:
            continue
        label = (
            str(year)
            if end.month == 1 and end.day == 1
            else f"{year} (to {end:%Y/%m/%d})"
        )
        out.append((label, start.strftime("%Y/%m/%d"), end.strftime("%Y/%m/%d")))
    return out


def write_summary(lines: list[str]) -> None:
    """Render the report on the run's own page, so nobody has to page a log."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main() -> int:
    gmail = build("gmail", "v1", credentials=credentials(), cache_discovery=False)

    protected, missing = resolve_protected(gmail)
    if missing:
        log(f"WARNING: protected label(s) not found, nothing protected by them: {missing}")

    rows = []
    total_candidates = 0
    all_ids: list[str] = []
    for label, after, before in year_buckets(BEFORE):
        ids = [i for i in list_ids(gmail, f"{BASE_QUERY} after:{after} before:{before}")
               if i not in protected]
        if not ids:
            continue
        rows.append((label, len(ids)))
        total_candidates += len(ids)
        all_ids.extend(ids)
        log(f"{label:>16}  {len(ids):>6}")

    summary = [
        "## Inbox Archive",
        "",
        f"**Mode:** {'LIVE — archiving' if ARCHIVE else 'REPORT ONLY — nothing touched'}",
        f"**Cut-off:** everything before `{BEFORE}`",
        f"**Query:** `{BASE_QUERY}` — already-read mail only, so nothing needing you can move",
        "",
        "| Year | Messages |",
        "|---|---:|",
    ]
    summary += [f"| {label} | {count:,} |" for label, count in rows]
    summary.append(f"| **Total** | **{total_candidates:,}** |")
    summary.append("")
    summary.append(f"Protected by label: {len(protected):,} messages "
                   f"({', '.join(PROTECTED_LABELS)}).")
    summary.append("Archiving removes INBOX only. Nothing is deleted; "
                   "everything stays in All Mail and stays searchable.")

    if not ARCHIVE:
        summary.append("")
        summary.append("Re-run with **Actually archive** ticked to apply. "
                       "Set a cap on the first live run.")
        write_summary(summary)
        log(f"\nREPORT ONLY. {total_candidates} message(s) would be archived. Nothing touched.")
        return 0

    if MAX_ARCHIVE:
        all_ids = all_ids[:MAX_ARCHIVE]
    done = 0
    for i in range(0, len(all_ids), BATCH):
        chunk = all_ids[i:i + BATCH]
        call(gmail.users().messages().batchModify(
            userId="me",
            body={"ids": chunk, "removeLabelIds": ["INBOX"]},
        ), units=50)
        done += len(chunk)
        log(f"archived {done}/{len(all_ids)}")

    summary.append("")
    summary.append(f"**Archived: {done:,} messages.**")
    if MAX_ARCHIVE and total_candidates > done:
        summary.append(f"{total_candidates - done:,} left — the cap stopped it. Re-run to continue.")
    write_summary(summary)
    log(f"\nDone. {done} message(s) archived. To undo: search in Gmail and move back to Inbox.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
