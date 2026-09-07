#!/usr/bin/env python3
"""
One-time helper: mint the refresh token GitHub Actions will use.

Run this once on your Mac. It opens a browser, you approve, and it prints the
three values to paste into GitHub as repository secrets. After that the laptop
is never needed again — the refresh token lets the Action authenticate on its
own, indefinitely, while your machine is shut.

    pip install -r requirements.txt
    python auth_setup.py /path/to/client_secret.json

The refresh token is a long-lived credential for your Google account. Do not
commit it, paste it into a chat, or email it to yourself. It goes straight into
GitHub Secrets and nowhere else.
"""

import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive",
]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    secrets_path = sys.argv[1]
    flow = InstalledAppFlow.from_client_secrets_file(secrets_path, SCOPES)
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
    print("Add these three as GitHub repository secrets")
    print("(repo -> Settings -> Secrets and variables -> Actions -> New secret)")
    print("=" * 70)
    print(f"\nGOOGLE_CLIENT_ID\n{client.get('client_id', '')}")
    print(f"\nGOOGLE_CLIENT_SECRET\n{client.get('client_secret', '')}")
    print(f"\nGOOGLE_REFRESH_TOKEN\n{creds.refresh_token}")
    print("\n" + "=" * 70)
    print("Then delete client_secret.json from your Downloads folder.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
