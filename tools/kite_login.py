"""Mint a Kite access token and hand it to GitHub Actions. Run once a morning.

This step cannot be automated away. Zerodha flushes every access token at
around 07:30 IST, and their position is that the exchange requires a user to
log in manually at least once a day, so they neither extend token validity nor
recommend scripting the login. What this does is make the manual part short:
you approve the login in a browser, paste one URL back, and the token lands in
the repo secret the 18:00 scan reads.

    export KITE_API_KEY=...        # Kite Connect app key
    export KITE_API_SECRET=...     # app secret, never leaves this machine
    python tools/kite_login.py --repo <owner>/<name>

Drop --repo to just print the token. Pushing the secret needs the GitHub CLI
(`gh auth login`) with permission to write secrets on that repo.

Do not commit either credential. The secret in particular signs the session
request; anyone holding it plus the key can mint tokens for your account.
"""
import argparse
import hashlib
import os
import subprocess
import sys
from urllib.parse import parse_qs, urlparse

import requests

LOGIN_URL = "https://kite.zerodha.com/connect/login?v=3&api_key={key}"
SESSION_URL = "https://api.kite.trade/session/token"


def request_token_from(pasted: str) -> str:
    """Accept the whole redirect URL or a bare token — after a login it is
    easier to copy the address bar than to pick the parameter out of it."""
    pasted = pasted.strip()
    if "request_token" in pasted:
        qs = parse_qs(urlparse(pasted).query)
        found = qs.get("request_token", [""])[0]
        if found:
            return found
    return pasted


def mint(api_key: str, api_secret: str, request_token: str) -> str:
    checksum = hashlib.sha256(
        (api_key + request_token + api_secret).encode()).hexdigest()
    r = requests.post(SESSION_URL,
                      data={"api_key": api_key,
                            "request_token": request_token,
                            "checksum": checksum},
                      headers={"X-Kite-Version": "3"},
                      timeout=20)
    body = r.json()
    if r.status_code != 200 or body.get("status") != "success":
        raise SystemExit(
            f"login failed {r.status_code}: {body.get('message', r.text[:200])}"
            "\nrequest tokens are single-use and expire in minutes — if you "
            "reused one, start again.")
    return body["data"]["access_token"]


def push_secret(repo: str, token: str) -> None:
    subprocess.run(
        ["gh", "secret", "set", "KITE_ACCESS_TOKEN",
         "--repo", repo, "--body", token],
        check=True)
    print(f"KITE_ACCESS_TOKEN updated on {repo}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", help="owner/name; omit to just print the token")
    args = ap.parse_args()

    api_key = os.environ.get("KITE_API_KEY")
    api_secret = os.environ.get("KITE_API_SECRET")
    if not api_key or not api_secret:
        raise SystemExit("set KITE_API_KEY and KITE_API_SECRET first")

    print("1. Open this and log in (TOTP is mandatory on Kite Connect):\n")
    print("   " + LOGIN_URL.format(key=api_key) + "\n")
    print("2. You land on your redirect URL. Paste the whole address here.\n")
    pasted = input("   redirect URL: ")

    token = mint(api_key, api_secret, request_token_from(pasted))
    if args.repo:
        push_secret(args.repo, token)
    else:
        print("\nKITE_ACCESS_TOKEN=" + token)
    print("Valid until the next ~07:30 IST flush.")


if __name__ == "__main__":
    if not sys.stdin.isatty():
        raise SystemExit("run this interactively — it needs a pasted URL")
    main()

