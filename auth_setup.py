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

import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

DRIVE = "https://www.googleapis.com/auth/drive"
READONLY_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", DRIVE]
SWEEP_SCOPES = ["https://www.googleapis.com/auth/gmail.modify", DRIVE]


def main() -> int:
    args = [a for a in sys.argv[1:]]
    sweep = "--sweep" in args
    args = [a for a in args if a != "--sweep"]

    if not args:
        print(__doc__)
        return 1

    secrets_path = args[0]
    scopes = SWEEP_SCOPES if sweep else READONLY_SCOPES
    token_name = "GOOGLE_REFRESH_TOKEN_SWEEP" if sweep else "GOOGLE_REFRESH_TOKEN"

    print(f"\nMinting {token_name}")
    print("Scopes: " + ", ".join(s.rsplit('/', 1)[-1] for s in scopes))
    if sweep:
        print("This token CAN move your mail to Trash. It is used only by the manual")
        print("Backlog Sweep workflow, never by the nightly Filing Clerk.\n")
    else:
        print("This token can only READ your mail.\n")

    flow = InstalledAppFlow.from_client_secrets_file(secrets_path, scopes)
    # access_type=offline + prompt=consent is what actually produces a refresh
    # token. Without prompt=consent Google will skip it on a repeat authorisation
    # and you will get an access token that expires in an hour.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not creds.refresh_token:
        print("\nNo refresh token came back. Revoke the app's access at")
        print("https://myaccount.google.com/permissions and run this again.")
        return 1

    with open(secrets_path) as fh:
        installed = json.load(fh)
    client = installed.get("installed") or installed.get("web") or {}

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
