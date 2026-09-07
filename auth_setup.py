#!/usr/bin/env python3
"""
One-time helper: mint the refresh tokens GitHub Actions will use.

Run this on your Mac. It opens a browser, you approve, and it prints the values to
paste into GitHub as repository secrets. After that the laptop is never needed again --
the refresh token lets the Action authenticate on its own, indefinitely, while your
machine is shut.

    pip install -r requirements.txt

    # the nightly Filing Clerk -- READ-ONLY on Gmail
    python auth_setup.py /path/to/client_secret.json

    # the manual Backlog Sweep -- can move mail to Trash
    python auth_setup.py --sweep /path/to/client_secret.json

    # mint, verify and store the sweep secret with NO copy-paste at all
    python auth_setup.py --sweep --token-only client_secret.json \
        | gh secret set GOOGLE_REFRESH_TOKEN_SWEEP

--token-only prints the refresh token and NOTHING else to stdout, so it can be piped
straight into `gh secret set`. Every message goes to stderr instead. Before it prints
anything it USES the token for a real API call; if that fails it prints nothing and exits
non-zero, so a broken token can never reach GitHub. This exists because a hand-pasted
token produced `invalid_grant: Bad Request` on 2026-09-07 -- a paste that picks up a line
break or drops a character looks fine on screen and fails in the runner.

TWO TOKENS, DELIBERATELY
------------------------
The Filing Clerk runs unattended every night and only ever reads mail, so its token is
minted with gmail.readonly and cannot move a message even if the code were wrong. The
Backlog Sweep can trash thousands of messages, so it gets its own gmail.modify token
under a different secret name, used only by a workflow a human starts by hand. Do not
collapse these into one token: the whole point is that the unattended job holds a
credential that cannot do damage.

A refresh token is a long-lived credential for your Google account. Do not commit it,
paste it into a chat, or email it to yourself. It goes straight into GitHub Secrets and
nowhere else.
"""

import contextlib
import json
import re
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

DRIVE = "https://www.googleapis.com/auth/drive"
READONLY_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", DRIVE]
SWEEP_SCOPES = ["https://www.googleapis.com/auth/gmail.modify", DRIVE]


def verify(refresh_token: str, client_id: str, client_secret: str, scopes: list[str]) -> bool:
    """Use the token for real before letting it anywhere near GitHub Secrets.

    A refresh that succeeds plus a Gmail profile call proves the token is complete, matches
    this client, and carries the scopes the workflow needs. Anything less and we print
    nothing, so the secret is never set to something broken.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None, refresh_token=refresh_token,
        client_id=client_id, client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token", scopes=scopes,
    )
    try:
        creds.refresh(Request())
        profile = build("gmail", "v1", credentials=creds,
                        cache_discovery=False).users().getProfile(userId="me").execute()
    except Exception as exc:
        print(f"VERIFICATION FAILED: {exc}", file=sys.stderr)
        return False
    print(f"Verified against {profile.get('emailAddress')} "
          f"({profile.get('messagesTotal')} messages).", file=sys.stderr)
    return True


def main() -> int:
    args = [a for a in sys.argv[1:]]
    sweep = "--sweep" in args
    token_only = "--token-only" in args
    args = [a for a in args if a not in ("--sweep", "--token-only")]

    if not args:
        print(__doc__)
        return 1

    secrets_path = args[0]
    scopes = SWEEP_SCOPES if sweep else READONLY_SCOPES
    token_name = "GOOGLE_REFRESH_TOKEN_SWEEP" if sweep else "GOOGLE_REFRESH_TOKEN"

    out = sys.stderr if token_only else sys.stdout
    print(f"\nMinting {token_name}", file=out)
    print("Scopes: " + ", ".join(s.rsplit('/', 1)[-1] for s in scopes), file=out)
    if sweep:
        print("This token CAN move your mail to Trash. It is used only by the manual", file=out)
        print("Backlog Sweep workflow, never by the nightly Filing Clerk.\n", file=out)
    else:
        print("This token can only READ your mail.\n", file=out)

    flow = InstalledAppFlow.from_client_secrets_file(secrets_path, scopes)
    # access_type=offline + prompt=consent is what actually produces a refresh
    # token. Without prompt=consent Google will skip it on a repeat authorisation
    # and you will get an access token that expires in an hour.
    #
    # run_local_server() PRINTS "Please visit this URL to authorize..." TO STDOUT.
    # Under --token-only that prompt and the auth URL went down the pipe ahead of the
    # token, so `gh secret set` stored the whole lot and the runner got invalid_grant --
    # while local verification passed, because it checked the token in memory rather than
    # the bytes being piped. Everything the flow prints now goes to stderr; stdout is
    # reserved for the token alone.
    with contextlib.redirect_stdout(sys.stderr):
        creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not creds.refresh_token:
        print("\nNo refresh token came back. Revoke the app's access at", file=out)
        print("https://myaccount.google.com/permissions and run this again.", file=out)
        return 1

    with open(secrets_path) as fh:
        installed = json.load(fh)
    client = installed.get("installed") or installed.get("web") or {}

    if not verify(creds.refresh_token, client.get("client_id", ""),
                  client.get("client_secret", ""), scopes):
        print("Nothing was printed and nothing was stored. Run it again.", file=sys.stderr)
        return 1

    if token_only:
        # Last line of defence: whatever reaches stdout must LOOK like a bare refresh
        # token - one opaque string, no whitespace, no URL, nothing else. If it does not,
        # print nothing rather than let `gh secret set` store rubbish.
        token = creds.refresh_token
        if (not re.fullmatch(r"[A-Za-z0-9._~+/=-]{20,}", token)
                or "http" in token or "\n" in token):
            print("REFUSING TO PRINT: the token does not look like a bare refresh token.",
                  file=sys.stderr)
            print("Nothing was written to stdout, so no secret was set.", file=sys.stderr)
            return 1
        # stdout carries the token and nothing else, so this can be piped into gh.
        sys.stdout.write(token)
        return 0

    print("\n" + "=" * 70)
    print("Add these as GitHub repository secrets")
    print("(repo -> Settings -> Secrets and variables -> Actions -> New secret)")
    print("=" * 70)
    print(f"\nGOOGLE_CLIENT_ID\n{client.get('client_id', '')}")
    print(f"\nGOOGLE_CLIENT_SECRET\n{client.get('client_secret', '')}")
    print(f"\n{token_name}\n{creds.refresh_token}")
    print("\n" + "=" * 70)
    if not sweep:
        print("CLIENT_ID and CLIENT_SECRET are shared by both workflows -- you only")
        print("need to add them once.")
    print("Then delete client_secret.json from your Downloads folder.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
