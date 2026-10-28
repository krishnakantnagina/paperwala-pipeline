"""One small database interface over two backends.

- ``SqliteDb``: a local file, for tests and local runs.
- ``TursoDb``: Turso's HTTP API (Hrana v2 ``/v2/pipeline``) through urllib.
  Not the libsql driver: it has no Windows wheels, and plain HTTPS needs no
  compiled code at all, so the laptop and the CI runner use the same path.

Both run the same SQL. ``batch()`` is all-or-nothing on both: Turso gets a
BEGIN / steps / COMMIT batch where each step only runs if the previous one
succeeded, and a final ROLLBACK that runs if any did not.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Iterable

Params = dict[str, Any] | None
Rows = list[dict[str, Any]]


class DbError(RuntimeError):
    pass


class ReadBudgetExceeded(DbError):
    """The run has read as many rows as it was allowed. A DbError, so every
    job already treats it as "give the paper back and stop the run"."""


class ReadMeter:
    """Rows Turso reports reading for this process, with a fuse.

    Turso's free plan allows 500M row reads a month and blocks EVERY database in
    the org once any metric is over (2026-10-28: the pipeline read 700M in a day
    and the live site went down). Each statement's reply carries ``rows_read``,
    so a run can count what it costs and stop at ``PIPELINE_READ_BUDGET`` (rows,
    default 500,000 per process; 0 turns the fuse off). Normal use is a few
    thousand: a claim reads about as many rows as it takes."""

    DEFAULT = 500_000

    def __init__(self, budget: int | None = None):
        if budget is None:
            budget = int(os.environ.get("PIPELINE_READ_BUDGET", self.DEFAULT))
        self.budget = budget
        self.read = self.written = self.statements = 0
        self._lock = threading.Lock()

    def add(self, read: int, written: int, statements: int = 1) -> None:
        with self._lock:
            self.read += read
            self.written += written
            self.statements += statements

    def check(self) -> None:
        if self.budget and self.read >= self.budget:
            raise ReadBudgetExceeded(
                f"read budget reached: {self.read:,} rows read (limit {self.budget:,}, "
                f"PIPELINE_READ_BUDGET). Stopping so the free Turso quota is not used up.")

    def summary(self) -> str:
        return (f"database use this run: {self.read:,} rows read, {self.written:,} written, "
                f"{self.statements:,} statements (read limit {self.budget:,})")


METER = ReadMeter()


def _count_use(result: dict) -> None:
    """Add the rows a reply says it read/wrote (execute: one result; batch: one per step)."""
    resp = (result or {}).get("response") or {}
    res = resp.get("result")
    if not res:                      # the trailing "close" reply carries no result
        return
    parts = [res] + [p for p in (res.get("step_results") or []) if p]
    METER.add(sum(int(p.get("rows_read") or 0) for p in parts),
              sum(int(p.get("rows_written") or 0) for p in parts))


class Db:
    def execute(self, sql: str, params: Params = None) -> Rows:
        raise NotImplementedError

    def batch(self, statements: Iterable[tuple[str, Params]]) -> list[Rows]:
        """Run statements atomically; return each one's rows."""
        raise NotImplementedError

    def close(self) -> None:
        pass


class SqliteDb(Db):
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")

    def execute(self, sql: str, params: Params = None) -> Rows:
        cur = self.conn.execute(sql, params or {})
        return [dict(r) for r in cur.fetchall()]

    def batch(self, statements):
        out = []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for sql, params in statements:
                out.append([dict(r) for r in self.conn.execute(sql, params or {}).fetchall()])
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return out

    def close(self) -> None:
        self.conn.close()


def _encode_value(v: Any) -> dict:
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, bytes):
        return {"type": "blob", "base64": base64.b64encode(v).decode()}
    return {"type": "text", "value": str(v)}


def _decode_value(v: dict) -> Any:
    t = v.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(v["value"])
    if t == "float":
        return float(v["value"])
    if t == "blob":
        return base64.b64decode(v.get("base64", ""))
    return v.get("value")


def _stmt(sql: str, params: Params) -> dict:
    stmt: dict = {"sql": sql}
    if params:
        # Turso rejects a named argument the statement does not use (SQLite
        # ignores it), so send only the ones referenced.
        used = set(re.findall(r":([A-Za-z_][A-Za-z0-9_]*)", sql))
        stmt["named_args"] = [{"name": f":{k}", "value": _encode_value(v)}
                              for k, v in params.items() if k in used]
    return stmt


def _rows(result: dict) -> Rows:
    cols = [c.get("name") for c in result.get("cols", [])]
    return [dict(zip(cols, (_decode_value(v) for v in row))) for row in result.get("rows", [])]


class TursoDb(Db):
    """Turso over HTTPS. Transient failures (network, 429, 5xx) are retried
    with backoff; a 4xx is a bug and raises at once.

    A retried write may already have committed on the first attempt. Callers
    in state.py are written for that: every write is guarded by a WHERE that
    makes a repeat a no-op, and they re-read when a write reports 0 rows.
    """

    RETRIES = 6

    def __init__(self, url: str, token: str, timeout: int = 60):
        url = url.replace("libsql://", "https://").rstrip("/")
        self.endpoint = url + "/v2/pipeline"
        self.token = token
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "TursoDb":
        url, token = os.environ.get("PIPELINE_DB_URL"), os.environ.get("PIPELINE_DB_TOKEN")
        if not url or not token:
            raise DbError("PIPELINE_DB_URL and PIPELINE_DB_TOKEN must be set")
        return cls(url, token)

    def _post(self, requests: list[dict]) -> list[dict]:
        METER.check()
        body = json.dumps({"requests": requests + [{"type": "close"}]}).encode()
        delay = 1.0
        for attempt in range(self.RETRIES):
            req = urllib.request.Request(self.endpoint, data=body, method="POST", headers={
                "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    results = json.loads(resp.read())["results"]
                for r in results:
                    _count_use(r)
                return results
            except urllib.error.HTTPError as exc:
                if exc.code != 429 and exc.code < 500:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                    raise DbError(f"HTTP {exc.code}: {detail}") from exc
                err: Exception = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                err = exc
            if attempt == self.RETRIES - 1:
                raise DbError(f"Turso unreachable after {self.RETRIES} tries: {err}") from err
            time.sleep(delay)
            delay = min(delay * 2, 30)
        raise AssertionError("unreachable")

    def execute(self, sql: str, params: Params = None) -> Rows:
        result = self._post([{"type": "execute", "stmt": _stmt(sql, params)}])[0]
        if result.get("type") != "ok":
            raise DbError(result.get("error", {}).get("message", "unknown error"))
        return _rows(result["response"]["result"])

    def batch(self, statements):
        stmts = list(statements)
        steps = [{"stmt": {"sql": "BEGIN"}}]
        for i, (sql, params) in enumerate(stmts, start=1):
            steps.append({"stmt": _stmt(sql, params), "condition": {"type": "ok", "step": i - 1}})
        last = len(steps) - 1
        steps.append({"stmt": {"sql": "COMMIT"}, "condition": {"type": "ok", "step": last}})
        steps.append({"stmt": {"sql": "ROLLBACK"},
                      "condition": {"type": "not", "cond": {"type": "ok", "step": last + 1}}})
        result = self._post([{"type": "batch", "batch": {"steps": steps}}])[0]
        if result.get("type") != "ok":
            raise DbError(result.get("error", {}).get("message", "unknown error"))
        batch = result["response"]["result"]
        errors = [e for e in batch.get("step_errors", []) if e]
        if errors:
            raise DbError(f"batch rolled back: {errors[0].get('message')}")
        return [_rows(r) if r else [] for r in batch["step_results"][1:1 + len(stmts)]]


def connect_live() -> "TursoDb":
    """The live site's database (Turso), for the publish job only."""
    url, token = os.environ.get("LIVE_DB_URL"), os.environ.get("LIVE_DB_TOKEN")
    if not url or not token:
        raise DbError("LIVE_DB_URL and LIVE_DB_TOKEN must be set to publish")
    # Accept the app's own DATABASE_URL form too: sqlite+libsql://host/?secure=true
    host = re.sub(r"^[a-z+]+://", "", url).split("/")[0].split("?")[0]
    return TursoDb("https://" + host, token)


def connect(target: str | None = None) -> Db:
    """A file path gives SqliteDb; nothing (or "turso") gives TursoDb from env."""
    if target and target != "turso":
        return SqliteDb(target)
    return TursoDb.from_env()
