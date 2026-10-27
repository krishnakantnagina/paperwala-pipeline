"""Google Drive v3 over urllib: download originals, upload cleaned copies.

No google-api-client: the pipeline needs five calls, and plain HTTPS keeps
the dependency list (and the CI image) small.

Unbreakable rules:
- Every download is written to ``.part``, hashed while streaming, and only
  renamed into place when complete; the caller compares the hash with the
  catalog's.
- Every upload is tagged with ``appProperties.pw_source`` (the original's file
  id) and ``pw_sha256``. Before uploading, the tag is searched: a retry after
  a lost reply finds the copy it already made instead of making a second one.
  Drive's search index lags by seconds (a deleted folder's file was still
  listed right after the delete), so a retry fired immediately can still miss
  that copy. The state database records the one file id that counts, and the
  reconcile job deletes any other file carrying the same ``pw_source``.
- After upload, Drive's own ``md5Checksum`` must equal the local file's.
- 429 / 5xx / network errors back off and retry; 401 refreshes the token once.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
CREDENTIALS_FILE = Path.home() / ".paperwala" / "gdrive_oauth.json"
RETRIES = 7
CHUNK = 1 << 20


class DriveError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


@dataclass
class Uploaded:
    file_id: str
    md5: str
    size: int
    reused: bool


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class DriveClient:
    def __init__(self, creds: dict):
        self.creds = creds
        self._token: str | None = None
        self._expires = 0.0

    @classmethod
    def from_env(cls) -> "DriveClient":
        """GDRIVE_OAUTH_JSON (CI secret) or ~/.paperwala/gdrive_oauth.json."""
        raw = os.environ.get("GDRIVE_OAUTH_JSON")
        if raw:
            return cls(json.loads(raw))
        if CREDENTIALS_FILE.exists():
            return cls(json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8")))
        raise DriveError("no Drive credentials: set GDRIVE_OAUTH_JSON or run pwpipe.drive_auth")

    # --- auth ------------------------------------------------------------

    def _access_token(self, force: bool = False) -> str:
        if self._token and not force and time.time() < self._expires - 120:
            return self._token
        body = urllib.parse.urlencode({
            "client_id": self.creds["client_id"], "client_secret": self.creds["client_secret"],
            "refresh_token": self.creds["refresh_token"], "grant_type": "refresh_token",
        }).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    self.creds.get("token_uri", "https://oauth2.googleapis.com/token"),
                    data=body), timeout=60) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            # invalid_grant = token revoked or expired: needs a human to log in again.
            raise DriveError(f"token refresh failed ({exc.code}): {detail}", exc.code) from exc
        self._token = data["access_token"]
        self._expires = time.time() + int(data.get("expires_in", 3600))
        return self._token

    # --- transport -------------------------------------------------------

    def _request(self, method: str, url: str, data: bytes | None = None,
                 headers: dict | None = None, stream_to=None):
        delay, refreshed = 1.0, False
        for attempt in range(RETRIES):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self._access_token()}", **(headers or {})})
            try:
                resp = urllib.request.urlopen(req, timeout=300)
                if stream_to is not None:
                    return stream_to(resp)
                with resp:
                    return resp.status, dict(resp.headers), resp.read()
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")[:500]
                if exc.code == 401 and not refreshed:
                    self._access_token(force=True)
                    refreshed = True
                    continue
                rate_limited = exc.code == 403 and ("rateLimit" in body or "userRateLimit" in body)
                if exc.code not in (429, 500, 502, 503, 504) and not rate_limited:
                    raise DriveError(f"{method} {url.split('?')[0]} -> {exc.code}: {body}",
                                     exc.code) from exc
                err: Exception = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                err = exc
            if attempt == RETRIES - 1:
                raise DriveError(f"{method} {url.split('?')[0]} failed after {RETRIES} tries: {err}")
            time.sleep(delay)
            delay = min(delay * 2, 60)
        raise AssertionError("unreachable")

    def _json(self, method: str, url: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        _, _, body = self._request(method, url, data, headers)
        return json.loads(body) if body else {}

    # --- reads -----------------------------------------------------------

    def about(self) -> dict:
        return self._json("GET", f"{API}/about?fields=user(emailAddress),storageQuota")

    def metadata(self, file_id: str,
                 fields: str = "id,name,size,md5Checksum,mimeType,trashed") -> dict:
        return self._json("GET", f"{API}/files/{file_id}?fields={fields}&supportsAllDrives=true")

    def download(self, file_id: str, dest: str | Path) -> tuple[str, int]:
        """Stream a file to ``dest``; returns (sha1, bytes). Atomic."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")

        def write(resp):
            h, n = hashlib.sha1(), 0
            with resp, open(tmp, "wb") as fh:
                while chunk := resp.read(CHUNK):
                    h.update(chunk)
                    fh.write(chunk)
                    n += len(chunk)
            return h.hexdigest(), n

        sha1, size = self._request(
            "GET", f"{API}/files/{file_id}?alt=media&supportsAllDrives=true", stream_to=write)
        tmp.replace(dest)
        return sha1, size

    def find_by_source(self, source_id: str) -> list[dict]:
        q = (f"appProperties has {{ key='pw_source' and value='{_q(source_id)}' }} "
             "and trashed = false")
        url = (f"{API}/files?q={urllib.parse.quote(q)}"
               "&fields=files(id,md5Checksum,size,appProperties,parents)&spaces=drive")
        return self._json("GET", url).get("files", [])

    # --- writes (only ever on files this app created: drive.file scope) ---

    def ensure_folder(self, parts: list[str], root: str = "root") -> str:
        parent = root
        for name in parts:
            q = (f"name = '{_q(name)}' and mimeType = '{FOLDER_MIME}' "
                 f"and '{_q(parent)}' in parents and trashed = false")
            found = self._json("GET", f"{API}/files?q={urllib.parse.quote(q)}"
                                      "&fields=files(id)&spaces=drive").get("files", [])
            if found:
                parent = found[0]["id"]
            else:
                parent = self._json("POST", f"{API}/files?fields=id", {
                    "name": name, "mimeType": FOLDER_MIME, "parents": [parent]})["id"]
        return parent

    def upload(self, path: str | Path, name: str, parent: str, source_id: str,
               sha256: str, anyone_reader: bool = True) -> Uploaded:
        path = Path(path)
        data = path.read_bytes()
        md5 = hashlib.md5(data).hexdigest()

        for existing in self.find_by_source(source_id):
            if existing.get("appProperties", {}).get("pw_sha256") == sha256 \
                    and existing.get("md5Checksum") == md5:
                return Uploaded(existing["id"], md5, len(data), reused=True)

        meta = {"name": name, "parents": [parent], "mimeType": "application/pdf",
                "appProperties": {"pw_source": source_id, "pw_sha256": sha256}}
        # Resumable session: one call to open it, one to send the bytes.
        status, headers, _ = self._request(
            "POST", f"{UPLOAD}?uploadType=resumable&fields=id",
            json.dumps(meta).encode(),
            {"Content-Type": "application/json; charset=UTF-8",
             "X-Upload-Content-Type": "application/pdf",
             "X-Upload-Content-Length": str(len(data))})
        session = headers.get("Location") or headers.get("location")
        if not session:
            raise DriveError("no resumable session URL returned")
        _, _, body = self._request("PUT", session, data, {"Content-Type": "application/pdf"})
        file_id = json.loads(body)["id"]

        remote = self.metadata(file_id)
        if remote.get("md5Checksum") != md5 or int(remote.get("size", -1)) != len(data):
            self.delete(file_id)
            raise DriveError(f"upload corrupted: md5 {remote.get('md5Checksum')} != {md5}")
        if anyone_reader:
            # Same sharing as the originals: anyone with the link can view.
            self._json("POST", f"{API}/files/{file_id}/permissions?fields=id",
                       {"type": "anyone", "role": "reader"})
        return Uploaded(file_id, md5, len(data), reused=False)

    def delete(self, file_id: str) -> None:
        self._request("DELETE", f"{API}/files/{file_id}")
