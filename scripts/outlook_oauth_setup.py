#!/usr/bin/env python3
"""One-time Outlook sign-in for the mailer: prints a refresh token for the
OUTLOOK_REFRESH_TOKEN secret.

    python scripts/outlook_oauth_setup.py <CLIENT_ID> | gh secret set OUTLOOK_REFRESH_TOKEN

Instructions go to the screen (stderr); only the token goes to stdout, so it can
be piped straight into `gh secret set` without being shown. Needs `requests`.
"""
import sys
import time

import requests

SCOPE = "https://outlook.office.com/SMTP.Send offline_access"


def say(text):
    print(text, file=sys.stderr, flush=True)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    client_id = sys.argv[1]
    tenant = sys.argv[2] if len(sys.argv) > 2 else "consumers"
    base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"

    r = requests.post(f"{base}/devicecode", data={"client_id": client_id, "scope": SCOPE}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"Could not start sign-in: {r.json().get('error_description', r.text)}")
    flow = r.json()
    say(f"\n1. Open {flow['verification_uri']}\n2. Enter the code {flow['user_code']}\n"
        "3. Sign in with the Outlook account that should send the emails and allow access.\n"
        "Waiting for you to finish...")

    interval = flow.get("interval", 5)
    deadline = time.time() + flow.get("expires_in", 900)
    while time.time() < deadline:
        time.sleep(interval)
        r = requests.post(f"{base}/token", timeout=30, data={
            "client_id": client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": flow["device_code"],
        })
        tokens = r.json()
        error = tokens.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error:
            sys.exit(f"Sign-in failed: {tokens.get('error_description', error)}")
        if "refresh_token" not in tokens:
            sys.exit("Signed in, but Microsoft returned no refresh token (is offline_access allowed?).")
        say("Signed in. Refresh token:" if sys.stdout.isatty() else "Signed in; token sent to the pipe.")
        print(tokens["refresh_token"])
        return
    sys.exit("The code expired; run the script again.")


if __name__ == "__main__":
    main()
