"""One-time Google Drive login: turns the OAuth client JSON into a refresh token.

    python -m pwpipe.drive_auth CLIENT_SECRET.json [--out PATH]

Opens the Google sign-in page; sign in with the account whose Drive will hold
the cleaned papers. Standard library only (loopback redirect + PKCE), so no
google-auth packages are needed anywhere in the pipeline.

Scopes are the narrowest that work:
- ``drive.readonly``: download the original papers, wherever they live.
- ``drive.file``: create and manage only the files this app created.
The pipeline therefore cannot modify or delete anything else in any Drive.

The token never expires on its own as long as the OAuth consent screen is
**In production** (in "Testing" Google revokes it after 7 days) and it is used
at least once every 6 months - the daily health job does that.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/drive.readonly",
          "https://www.googleapis.com/auth/drive.file"]
DEFAULT_OUT = Path.home() / ".paperwala" / "gdrive_oauth.json"
TIMEOUT_SECONDS = 600


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=60) as resp:
        return json.loads(resp.read())


def _get_json(url: str, access_token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def login(client_file: Path) -> dict:
    client = json.loads(client_file.read_text(encoding="utf-8"))
    if "installed" not in client:
        raise SystemExit("this must be a *Desktop app* OAuth client JSON ('installed')")
    client = client["installed"]

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()) \
        .rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] != state:
                self.send_response(400)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Signed in. You can close this tab." if "code" in result else \
                f"Sign-in failed: {result.get('error', 'unknown')}"
            self.wfile.write(f"<p style='font:16px sans-serif'>{msg}</p>".encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect_uri = f"http://localhost:{server.server_port}"
    auth_url = client["auth_uri"] + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect_uri,
        "response_type": "code", "scope": " ".join(SCOPES),
        # offline + consent: Google only returns a refresh token on consent.
        "access_type": "offline", "prompt": "consent select_account",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    })
    print("Opening the Google sign-in page. If it does not open, visit:\n" + auth_url,
          flush=True)
    webbrowser.open(auth_url)

    thread = threading.Thread(target=server.handle_request, daemon=True)
    server.timeout = TIMEOUT_SECONDS
    thread.start()
    thread.join(TIMEOUT_SECONDS + 5)
    server.server_close()
    if "code" not in result:
        raise SystemExit(f"no sign-in received: {result.get('error', 'timed out')}")

    token = _post_form(client["token_uri"], {
        "code": result["code"], "client_id": client["client_id"],
        "client_secret": client["client_secret"], "redirect_uri": redirect_uri,
        "grant_type": "authorization_code", "code_verifier": verifier,
    })
    if "refresh_token" not in token:
        raise SystemExit("Google returned no refresh token; remove the app's access at "
                         "myaccount.google.com/permissions and run this again")
    granted = set(token.get("scope", "").split())
    missing = set(SCOPES) - granted
    if missing:
        raise SystemExit(f"these permissions were not granted (tick both boxes): {sorted(missing)}")

    about = _get_json("https://www.googleapis.com/drive/v3/about?fields=user(emailAddress),"
                      "storageQuota", token["access_token"])
    return {
        "client_id": client["client_id"], "client_secret": client["client_secret"],
        "token_uri": client["token_uri"], "refresh_token": token["refresh_token"],
        "scopes": SCOPES, "account": about["user"]["emailAddress"],
        "storage": about.get("storageQuota", {}),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pwpipe.drive_auth")
    ap.add_argument("client_secret")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args(argv)

    creds = login(Path(args.client_secret))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    tmp.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    if os.name != "nt":
        tmp.chmod(0o600)
    tmp.replace(out)

    quota = creds["storage"]
    gb = lambda v: f"{int(v) / 1e9:.1f} GB" if v else "unlimited"  # noqa: E731
    print(f"Signed in as {creds['account']}")
    print(f"Drive storage: {gb(quota.get('usage'))} used of {gb(quota.get('limit'))}")
    print(f"Saved to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
