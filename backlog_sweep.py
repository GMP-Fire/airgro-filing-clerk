#!/usr/bin/env python3
"""
Backlog Sweep - clears the historic Gmail backlog in bulk.

Why this exists
---------------
The inbox carries roughly 28,000 threads of accumulated marketing. The Gmail Steward
clears 50-100 per run and new mail replaces them, so it can never catch up. The MCP
connector has no bulk operation. The Gmail API does: users.messages.batchModify takes
1,000 ids per call, so the whole backlog is a few dozen calls.

WHY batchModify AND NOT batchDelete
-----------------------------------
batchDelete destroys mail permanently and irreversibly. batchModify adding the TRASH
label does the same visible thing, in the same number of calls, and stays recoverable in
Gmail for 30 days. There is no reason to use batchDelete here and every reason not to.

=============================================================================
THE MISTAKE THIS SCRIPT IS DESIGNED NOT TO REPEAT
=============================================================================
On 2026-09-07 a set of hand-written Gmail filters trashed eight threads Andrew had
personally flagged. The cause was not a bad sender list. It was that Gmail filters match
a SENDER and act unconditionally, while the Steward's real protection for that mail is a
SUBJECT rule evaluated earlier in its order of precedence.

The first version of this script had exactly the same hole, and a check against the live
mailbox found it before it ran:

  no-reply@spotify.com          on trash.from_any -> would have trashed
                                "Spotify Premium Payment Failure: Update your payment
                                details today" and two Spotify receipts, all in the inbox
  donotreply@23andme.com        -> "23andMe Password Reset Request" and
                                "Your 23andMe Password Has Been Changed", both in the inbox
  hello@breathe.calm.com        -> "Your Calm subscription receipt", in the inbox

Every one of those is caught by keep_inbox.security.subject_any or keep_inbox.money
.subject_any. A sender-only sweep never sees them. So this script does not sweep on
sender alone: it reads the SUBJECT of every candidate message and re-applies the
Steward's subject vocabulary in code before anything is touched.

=============================================================================
THE GATES
=============================================================================
  1. LABEL CHECK   Every never-touch label must exist in the mailbox, resolved by name to
                   its real id. Gmail silently ignores -label:"Typo", which would make the
                   clause protect nothing.
  2. SENDER GATE   Hard abort if a sender is on the trash list AND on protect,
                   keep_inbox.*.from_any, documents_of_record or reading. That is a
                   contradiction in the rules, not something to resolve automatically.
  3. SUBJECT GATE  Every candidate message's subject is checked against the full protected
                   vocabulary (protect + money + security + bookings + suspicious). Any
                   hit is held back and reported, never swept. THIS IS THE ONE THAT WOULD
                   HAVE CAUGHT THE FILTERS.
  4. LABEL GATE    Any message carrying the *Action this Day or Steward/Suspicious label
                   id is held back, checked against raw label ids rather than trusting the
                   query - gmail-rules.json labels_note warns about exactly this.
  5. REPORT FIRST  SWEEP is off unless explicitly set. Report mode writes nothing.
  6. TRASH ONLY    Recoverable for 30 days. batchDelete is never called.

cutlist-keep.json is deliberately NOT an abort. gmail-rules.json documents five senders
(gforcegolf, theoxpecker, hphpublishing, firefinchapp, golfrsa) that sit on both lists
because Andrew put them on the trash list himself, and states that trash.from_any wins.
They are swept, and named in their own block in the report so the decision stays visible.

CREDENTIALS
-----------
Uses GOOGLE_REFRESH_TOKEN_SWEEP, a separate token minted with gmail.modify. The nightly
Filing Clerk keeps its own gmail.readonly token and stays structurally incapable of
touching mail.
"""

from __future__ import annotations

import json
import os
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
]

RULES_NAME = "gmail-rules.json"
KEEP_NAME = "cutlist-keep.json"

SWEEP = os.environ.get("SWEEP", "").lower() in ("1", "true", "yes")
ONLY_SENDERS = [s.strip().lower() for s in os.environ.get("ONLY_SENDERS", "").split(",") if s.strip()]
MAX_SENDERS = int(os.environ.get("MAX_SENDERS", "0"))      # 0 = no limit
MAX_SWEEP = int(os.environ.get("MAX_SWEEP", "0"))          # 0 = no limit; hard cap on messages trashed
BATCH = 1000          # batchModify ids per call
META_BATCH = 100      # metadata gets per HTTP batch

# ---------------------------------------------------------------------------
# SUPPLEMENTARY SUBJECT GUARD
# ---------------------------------------------------------------------------
# The Steward's own subject vocabulary is necessary but NOT sufficient for a bulk sweep,
# and testing against the live mailbox proved it. Real subjects sitting in the inbox that
# gmail-rules.json does NOT catch:
#
#   "Spotify Premium Payment Failure: Update your payment details today"
#        keep_inbox.money has "payment failed", not "payment failure"
#   "Your 23andMe Password Has Been Changed"
#        keep_inbox.security has "password reset", not a bare "password"
#   "payCity | Traffic Fine Alert - View and Pay in Seconds"
#        nothing in the vocabulary covers traffic fines at all
#   "Spotify Receipt" / "Your Calm subscription receipt"
#        keep_inbox.money has "invoice", "remittance", "pro forma" - not "receipt"
#
# The Steward gets away with those gaps because it looks at 50-100 threads a run with
# judgement behind it. A sweep touching thousands of messages at once has no judgement, so
# it needs a wider net. These phrases are ADDITIVE - they widen what this script refuses to
# touch and change nothing about how the Steward behaves.
#
# The asymmetry that sets the bias: a marketing mail held back costs nothing, it just waits
# for the Steward. A trashed password reset, traffic fine or payment failure is a real loss.
# When in doubt, add the phrase.
SWEEP_EXTRA_SUBJECTS = [
    # money and records
    "receipt", "payment failure", "payment problem", "payment issue", "payment method",
    "billing", "card declined", "card expiring", "update your payment", "update your card",
    "refund", "credit note", "statement", "balance", "arrears", "paid",
    # fines and officialdom
    "traffic fine", "fine alert", "penalty", "infringement", "summons", "court",
    "licence", "license renewal", "municipal",
    # security and account integrity
    "password", "passcode", "sign-in", "signed in", "sign in to", "log in to", "logged in",
    "verification", "verify", "authenticate", "authentication", "one-time", "recovery code",
    "account locked", "account suspended", "unusual", "unrecognised", "unrecognized",
    "registration attempt", "account created",
    "was accessed", "security",
    # orders, delivery and things already paid for
    "your order", "order confirm", "order number", "order #", "has shipped", "dispatched",
    "despatched", "out for delivery", "on its way", "tracking", "waybill", "collection ready",
    "ready for collection", "return request", "exchange request",
    # subscriptions and renewals
    "subscription", "auto-renew", "will renew", "renewing", "renewal", "membership",
    "expiring soon", "cancellation", "has been cancelled", "has been canceled",
    # travel and tickets
    "booking", "reservation", "boarding pass", "e-ticket", "your ticket", "itinerary",
    "check-in", "flight",
]

# EXACT label names as they exist in the mailbox. Verified against users.labels.list on
# 2026-09-07: "*Action this Day" = Label_2308127286056104914, "Steward/Suspicious" = Label_38.
# The first version wrote "Steward-Suspicious" with a hyphen. Gate 1 caught it on the first
# run; a query clause naming it would have protected nothing at all, silently.
NEVER_TOUCH_LABELS = ["*Action this Day", "Steward/Suspicious"]
NEVER_TOUCH = (
    '-is:starred '
    '-label:"*Action this Day" '
    '-in:sent -in:draft -in:trash -in:spam '
    '-label:"Steward/Suspicious"'
)


def log(msg: str = "") -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- auth


def credentials() -> Credentials:
    missing = [
        k for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN_SWEEP")
        if not os.environ.get(k)
    ]
    if missing:
        sys.exit(
            f"Missing secrets: {', '.join(missing)}\n"
            "GOOGLE_REFRESH_TOKEN_SWEEP is a separate token from the Filing Clerk's. "
            "Mint it with: python auth_setup.py --sweep <client_secret.json>"
        )
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN_SWEEP"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    try:
        creds.refresh(Request())
    except Exception as exc:
        sys.exit(
            f"Auth failed: {exc}\n"
            "If this mentions scope, GOOGLE_REFRESH_TOKEN_SWEEP was minted read-only. "
            "Re-run: python auth_setup.py --sweep <client_secret.json>"
        )
    return creds


# --------------------------------------------------------------------------- config


def drive_find(svc, name: str) -> str | None:
    safe = name.replace("'", "\\'")
    res = svc.files().list(
        q=f"name = '{safe}' and trashed = false",
        fields="files(id,name,modifiedTime)", pageSize=10,
    ).execute()
    files = res.get("files", [])
    if not files:
        return None
    if len(files) > 1:
        listing = "\n".join(f"  {f['id']}  {f.get('modifiedTime')}" for f in files)
        sys.exit(f"ABORTING: {len(files)} files named {name}:\n{listing}\nExactly one must exist.")
    return files[0]["id"]


def load_config(drive):
    rules_id = drive_find(drive, RULES_NAME)
    if not rules_id:
        sys.exit(f"Cannot find {RULES_NAME} in Drive.")
    rules = json.loads(drive.files().get_media(fileId=rules_id).execute().decode("utf-8"))
    keep_id = drive_find(drive, KEEP_NAME)
    keep_raw = json.loads(
        drive.files().get_media(fileId=keep_id).execute().decode("utf-8")
    ) if keep_id else {}
    return rules, keep_raw


def collect_protected_senders(rules: dict) -> set[str]:
    """Senders whose presence on the trash list is a contradiction in the rules.

    cutlist-keep is deliberately excluded - see the module docstring.
    """
    out: set[str] = set()

    reading = rules.get("reading", {})
    for s in reading.get("from_any", []):
        out.add(s.lower())
    for o in reading.get("overrides", []):
        if isinstance(o, dict) and o.get("from"):
            out.add(o["from"].lower())

    prot = rules.get("protect", {})
    for key in ("from_domains", "from_addresses", "from_contains"):
        for s in prot.get(key, []):
            out.add(s.lower())

    for section in rules.get("keep_inbox", {}).values():
        if isinstance(section, dict):
            for s in section.get("from_any", []):
                out.add(s.lower())

    for s in rules.get("documents_of_record", {}).get("from_any", []):
        out.add(s.lower())

    for rule in rules.get("autofile", {}).get("rules", []):
        for s in rule.get("from_any", []):
            out.add(s.lower())

    out.discard("")
    return out


def collect_protected_subjects(rules: dict) -> set[str]:
    """The Steward's subject vocabulary that outranks the trash rule.

    protect, keep_inbox.money, keep_inbox.security, keep_inbox.bookings and suspicious are
    all evaluated BEFORE trash in the Steward's order of precedence, so a subject match in
    any of them means the mail must stay put regardless of who sent it.
    """
    out: set[str] = set()
    for s in rules.get("protect", {}).get("subject_any", []):
        out.add(s.lower())
    for section in rules.get("keep_inbox", {}).values():
        if isinstance(section, dict):
            for s in section.get("subject_any", []):
                out.add(s.lower())
    for s in rules.get("suspicious", {}).get("subject_any", []):
        out.add(s.lower())
    # Additive - see SWEEP_EXTRA_SUBJECTS above for why the Steward's list is not enough.
    for s in SWEEP_EXTRA_SUBJECTS:
        out.add(s.lower())
    out.discard("")
    return out


def cutlist_senders(keep_raw: dict) -> set[str]:
    out: set[str] = set()
    for entry in keep_raw.get("keep", []):
        if isinstance(entry, dict) and entry.get("addr"):
            out.add(entry["addr"].lower())
    return out


def find_leaks(senders: list[str], protected: set[str]) -> list[tuple[str, str]]:
    """Substring both ways, so a domain on one list catches a full address on the other."""
    leaks = []
    for s in senders:
        for p in protected:
            if p and s and (p in s or s in p):
                leaks.append((s, p))
    return leaks


def protected_subject_hit(subject: str, vocabulary: set[str]) -> str | None:
    low = (subject or "").lower()
    for phrase in vocabulary:
        if phrase in low:
            return phrase
    return None


# --------------------------------------------------------------------------- gmail


def resolve_labels(gmail) -> dict[str, str]:
    """Map never-touch label names to real ids, or abort.

    gmail-rules.json labels_note: search returns raw label ids, not names, and a
    -label: clause naming a label that does not exist matches nothing silently.
    """
    labels = gmail.users().labels().list(userId="me").execute().get("labels", [])
    by_name = {l["name"]: l["id"] for l in labels}
    absent = [n for n in NEVER_TOUCH_LABELS if n not in by_name]
    if absent:
        sys.exit(
            "ABORTING - never-touch label(s) not found in the mailbox: "
            + ", ".join(repr(a) for a in absent)
            + "\nGmail ignores a -label: clause naming a label that does not exist, so the "
            "protection would silently not apply. Nothing was touched."
        )
    resolved = {n: by_name[n] for n in NEVER_TOUCH_LABELS}
    log("Gate 1 - labels resolved: " + ", ".join(f"{n} = {i}" for n, i in resolved.items()))
    return resolved


def message_ids(gmail, query: str, cap: int = 50000) -> list[str]:
    ids, page = [], None
    while True:
        res = gmail.users().messages().list(
            userId="me", q=query, maxResults=500, pageToken=page
        ).execute()
        ids.extend(m["id"] for m in res.get("messages", []))
        page = res.get("nextPageToken")
        if not page or len(ids) >= cap:
            break
    return ids


def fetch_metadata(gmail, ids: list[str]) -> dict[str, dict]:
    """Subject + labelIds for every id, in batches of META_BATCH."""
    out: dict[str, dict] = {}

    def collect(request_id, response, exception):
        if exception is not None or not response:
            return
        hdrs = {h["name"].lower(): h["value"]
                for h in response.get("payload", {}).get("headers", [])}
        out[response["id"]] = {
            "subject": hdrs.get("subject", ""),
            "date": hdrs.get("date", "")[:16],
            "labelIds": response.get("labelIds", []),
        }

    for i in range(0, len(ids), META_BATCH):
        batch = gmail.new_batch_http_request(callback=collect)
        for mid in ids[i:i + META_BATCH]:
            batch.add(gmail.users().messages().get(
                userId="me", id=mid, format="metadata",
                metadataHeaders=["Subject", "Date"],
            ))
        try:
            batch.execute()
        except HttpError as exc:
            log(f"    metadata batch failed ({exc}) - those messages are held back, not swept")
    return out


def partition(ids: list[str], meta: dict[str, dict], vocabulary: set[str],
              label_ids: dict[str, str]) -> tuple[list[str], list[tuple[str, str, str]]]:
    """Split candidates into (sweepable, held).

    A message with no metadata is HELD, never swept - if we could not read it, we do not
    know whether it is protected.
    """
    guarded = set(label_ids.values())
    sweepable: list[str] = []
    held: list[tuple[str, str, str]] = []   # (subject, date, reason)

    for mid in ids:
        m = meta.get(mid)
        if m is None:
            held.append(("(metadata unavailable)", "", "could not read - held by default"))
            continue
        carried = guarded.intersection(m["labelIds"])
        if carried:
            names = [n for n, i in label_ids.items() if i in carried]
            held.append((m["subject"][:66], m["date"], "label " + ", ".join(names)))
            continue
        hit = protected_subject_hit(m["subject"], vocabulary)
        if hit:
            held.append((m["subject"][:66], m["date"], f'subject "{hit}"'))
            continue
        sweepable.append(mid)
    return sweepable, held


# --------------------------------------------------------------------------- main


def main() -> int:
    creds = credentials()
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    label_ids = resolve_labels(gmail)                                     # GATE 1

    rules, keep_raw = load_config(drive)
    log(f"Loaded {RULES_NAME} v{rules.get('version')} (mode: {rules.get('mode')})")

    senders = [s.lower() for s in rules.get("trash", {}).get("from_any", [])]
    protected_senders = collect_protected_senders(rules)
    vocabulary = collect_protected_subjects(rules)
    bs_extra = {s.lower() for s in SWEEP_EXTRA_SUBJECTS}
    cutlist = cutlist_senders(keep_raw)

    leaks = find_leaks(senders, protected_senders)                        # GATE 2
    if leaks:
        log("\nABORTING - these senders are on the trash list AND on a protected list:")
        for s, p in leaks:
            log(f"  {s}  <->  {p}")
        log("That is a contradiction in gmail-rules.json. Resolve it there. Nothing was touched.")
        return 1
    log(f"Gate 2 - sender gate passed: {len(senders)} to sweep, "
        f"{len(protected_senders)} protected, no contradiction.")

    overlap = sorted({s for s in senders for c in cutlist if c in s or s in c})
    if overlap:
        log(f"\nNOTE - {len(overlap)} sender(s) appear on BOTH trash.from_any and cutlist-keep.json.")
        log("gmail-rules.json says trash.from_any wins where you put them there yourself,")
        log("so these ARE swept. Listed so the decision stays visible:")
        for s in overlap:
            log(f"  {s}")

    log(f"\nGate 3 - subject vocabulary: {len(vocabulary)} protected phrases "
        f"({len(vocabulary) - len(bs_extra)} from gmail-rules.json + {len(bs_extra)} sweep-specific)")
    log("         checked against every candidate's subject before anything is touched.")

    if ONLY_SENDERS:
        senders = [s for s in senders if any(o in s for o in ONLY_SENDERS)]
        log(f"\nONLY_SENDERS filter active: {len(senders)} sender(s).")
    if MAX_SENDERS:
        senders = senders[:MAX_SENDERS]

    mode = "SWEEP - mail WILL be moved to Trash" if SWEEP else "REPORT ONLY - nothing will be touched"
    log(f"\nMODE: {mode}")
    log(f"Never-touch filter on every query: {NEVER_TOUCH}\n")
    log(f"{'sweep':>7} {'held':>6}  sender")
    log(f"{'-'*7} {'-'*6}  {'-'*58}")

    plan = []          # (n_sweep, sender, ids)
    all_held = []      # (sender, subject, date, reason)
    tot_sweep = tot_held = 0

    for s in senders:
        query = f"from:{s} {NEVER_TOUCH}"
        try:
            ids = message_ids(gmail, query)
        except HttpError as exc:
            log(f"{'ERR':>7} {'':>6}  {s}  ({exc})")
            continue
        if not ids:
            continue
        meta = fetch_metadata(gmail, ids)
        sweepable, held = partition(ids, meta, vocabulary, label_ids)     # GATES 3 + 4
        tot_sweep += len(sweepable)
        tot_held += len(held)
        for subj, date, reason in held:
            all_held.append((s, subj, date, reason))
        if sweepable:
            plan.append((len(sweepable), s, sweepable))
        log(f"{len(sweepable):>7} {len(held):>6}  {s}")

    log(f"{'-'*7} {'-'*6}  {'-'*58}")
    log(f"{tot_sweep:>7} {tot_held:>6}  TOTAL across {len(senders)} senders\n")

    if all_held:
        log(f"HELD BACK - {len(all_held)} message(s) matched a sender on the trash list but")
        log("are protected by subject or label. These are NEVER swept:\n")
        for sender, subj, date, reason in all_held[:60]:
            log(f"  [{reason}]")
            log(f"      {date}  {subj}")
            log(f"      from {sender}")
        if len(all_held) > 60:
            log(f"  ... and {len(all_held) - 60} more")
        log()

    if not plan:
        log("Nothing to sweep.")
        return 0

    plan.sort(reverse=True)
    log("Sample of what WOULD be swept, from the five largest senders:\n")
    for _n, s, ids in plan[:5]:
        log(f"  {s}")
        meta = fetch_metadata(gmail, ids[:3])
        for mid in ids[:3]:
            m = meta.get(mid, {})
            log(f"      {m.get('date','')}  {m.get('subject','')[:66]}")
        log()

    if not SWEEP:                                                          # GATE 5
        log("REPORT ONLY. Nothing was touched.")
        log("Read the held-back list above first - that is the safety net doing its job.")
        log("To sweep: re-run with SWEEP=1. To sweep one sender first, set ONLY_SENDERS.")
        log("To cap the first live run, set MAX_SWEEP (e.g. 500).")
        return 0

    log("SWEEPING - adding the TRASH label. Recoverable in Gmail for 30 days.\n")
    swept = 0
    for _n, s, ids in plan:                                                # GATE 6
        if MAX_SWEEP and swept >= MAX_SWEEP:
            log(f"  MAX_SWEEP of {MAX_SWEEP} reached - stopping here.")
            break
        if MAX_SWEEP:
            ids = ids[:MAX_SWEEP - swept]
        for i in range(0, len(ids), BATCH):
            chunk = ids[i:i + BATCH]
            gmail.users().messages().batchModify(
                userId="me",
                body={"ids": chunk,
                      "addLabelIds": ["TRASH"],
                      "removeLabelIds": ["INBOX", "UNREAD"]},
            ).execute()
            swept += len(chunk)
        log(f"  swept {len(ids):>6} from {s}")

    log(f"\nDone. {swept} messages moved to Trash. {tot_held} held back by the subject and label gates.")
    log("Recoverable for 30 days: search in:trash in Gmail.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
