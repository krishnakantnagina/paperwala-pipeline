"""Per-paper state: which stage each paper is at, and who is working on it.

``stage`` is the last step a paper *completed*; ``status`` says what may
happen next:

    ready   waiting for the next step (maybe after a backoff: lease_until)
    leased  a job holds it until lease_until
    review  waiting for a person (review_reasons says why)
    dead    failed MAX_ATTEMPTS times; listed on the dashboard
    done    nothing more to do

Unbreakable by construction:

- **Claiming is one UPDATE ... RETURNING**, so two jobs can never claim the
  same paper, without sharding or locks.
- **Leases expire.** A job that crashes, is cancelled or hits the 6-hour cap
  simply stops renewing; its papers become claimable again.
- **Every write is fenced** on (stage, status='leased', lease_owner). A job
  whose lease expired and was taken over cannot overwrite the newer result:
  its write matches 0 rows and it is told so.
- **Retries are safe.** If a write's first attempt committed but the reply was
  lost, the retry matches 0 rows; ``advance`` then re-reads the row and treats
  "already at the target with my output" as success.
- **Every transition is logged** in ``events`` in the same transaction.
"""

from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass
from pathlib import Path

from .db import Db

SCHEMA_VERSION = 3
MAX_ATTEMPTS = 5
LEASE_SECONDS = 30 * 60
BACKOFF_SECONDS = [60, 5 * 60, 30 * 60, 2 * 3600]   # after attempt 1, 2, 3, 4

STAGES = ["discovered", "downloaded", "processed", "staged", "approved", "published",
          "live_verified"]
STATUSES = ["ready", "leased", "review", "dead", "done"]

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS papers (
        drive_id TEXT PRIMARY KEY,
        catalog TEXT NOT NULL,
        university TEXT, course TEXT, branch TEXT, grp TEXT, title TEXT, code TEXT,
        session TEXT, month TEXT, year TEXT, path TEXT,
        catalog_sha1 TEXT, catalog_bytes INTEGER,
        stage TEXT NOT NULL DEFAULT 'discovered',
        status TEXT NOT NULL DEFAULT 'ready',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        lease_owner TEXT, lease_until INTEGER,
        source_sha256 TEXT, output_sha256 TEXT,
        pipeline_version TEXT, blocklist_version TEXT,
        auto_ok INTEGER, review_reasons TEXT, report_ref TEXT,
        updated_at INTEGER NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS ix_papers_work ON papers (stage, status, lease_until)""",
    """CREATE INDEX IF NOT EXISTS ix_papers_catalog ON papers (catalog, stage, status)""",
    """CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        drive_id TEXT NOT NULL, at INTEGER NOT NULL, owner TEXT,
        from_stage TEXT, to_stage TEXT, status TEXT, detail TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS ix_events_paper ON events (drive_id, at)""",
]

# Upgrades, by the version they produce. Each runs in its own transaction
# together with the version bump, so an upgrade is never half-applied.
_UPGRADES = {
    2: [
        # The cleaned copy in the new Drive, and where its report/crops live.
        "ALTER TABLE papers ADD COLUMN output_file_id TEXT",
        "ALTER TABLE papers ADD COLUMN output_bytes INTEGER",
        "ALTER TABLE papers ADD COLUMN crops_ref TEXT",
    ],
    3: [
        # Papers a person sent back (requeue) jump the queue.
        "ALTER TABLE papers ADD COLUMN priority INTEGER NOT NULL DEFAULT 0",
    ],
}

# Fields a stage may record on a paper as it advances.
RESULT_FIELDS = {"source_sha256", "output_sha256", "pipeline_version", "blocklist_version",
                 "auto_ok", "review_reasons", "report_ref", "output_file_id", "output_bytes",
                 "crops_ref"}


def now() -> int:
    return int(time.time())


def migrate(db: Db) -> None:
    rows = []
    try:
        rows = db.execute("SELECT value FROM meta WHERE key = 'schema_version'")
    except Exception:
        pass  # no meta table yet
    current = int(rows[0]["value"]) if rows else 0
    if current > SCHEMA_VERSION:
        raise RuntimeError(f"database schema {current} is newer than this code ({SCHEMA_VERSION})")
    bump = ("INSERT INTO meta (key, value) VALUES ('schema_version', :v) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value")
    if current == 0:
        db.batch([(sql, None) for sql in _SCHEMA] + [(bump, {"v": "1"})])
        current = 1
    for version in range(current + 1, SCHEMA_VERSION + 1):
        db.batch([(sql, None) for sql in _UPGRADES[version]] + [(bump, {"v": str(version)})])


# Written in the same transaction as the update it records, and only if that
# update matched a row (changes() is the row count of the previous statement).
_EVENT_IF_CHANGED = (
    "INSERT INTO events (drive_id, at, owner, from_stage, to_stage, status, detail) "
    "SELECT :e_d, :e_at, :e_o, :e_f, :e_t, :e_s, :e_x WHERE changes() > 0")
LEASE_EXPIRED = "lease expired (worker died or timed out)"


def _event(drive_id, owner, from_stage, to_stage, status, detail="") -> dict:
    return {"e_d": drive_id, "e_at": now(), "e_o": owner, "e_f": from_stage, "e_t": to_stage,
            "e_s": status, "e_x": detail[:2000]}


# --- seeding ---------------------------------------------------------------

@dataclass
class SeedStats:
    rows: int = 0
    inserted: int = 0
    no_drive_id: int = 0


def _catalog_rows(catalog_dir: Path) -> list[dict]:
    papers = catalog_dir / "papers_full.csv"
    if not papers.exists():
        raise FileNotFoundError(papers)
    manifest: dict[str, dict] = {}
    mpath = catalog_dir / "arranged_manifest.csv"
    if mpath.exists():
        with open(mpath, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                manifest[row.get("arranged_path", "").replace("\\", "/")] = row
    with open(papers, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        m = manifest.get(row.get("path", "").replace("\\", "/"), {})
        row["_sha1"] = m.get("sha1") or None
        row["_bytes"] = int(m["bytes"]) if (m.get("bytes") or "").isdigit() else None
    return rows


def seed_catalog(db: Db, catalog_dir: str | Path, catalog: str, chunk: int = 200) -> SeedStats:
    """Add a catalog's papers. Re-running refreshes catalog fields only;
    a paper's stage, status and results are never touched by a re-seed."""
    stats = SeedStats()
    rows = _catalog_rows(Path(catalog_dir))
    stats.rows = len(rows)
    pending = []
    for row in rows:
        drive_id = (row.get("driveId") or "").strip()
        if not drive_id:
            stats.no_drive_id += 1
            continue
        pending.append({
            "d": drive_id, "c": catalog, "u": row.get("university"), "co": row.get("course"),
            "b": row.get("branch"), "g": row.get("group"), "ti": row.get("title"),
            "cd": row.get("code"), "se": row.get("session"), "mo": row.get("month"),
            "y": row.get("year"), "p": row.get("path"), "sh": row["_sha1"],
            "by": row["_bytes"], "at": now(),
        })
    sql = """INSERT INTO papers (drive_id, catalog, university, course, branch, grp, title, code,
                                 session, month, year, path, catalog_sha1, catalog_bytes, updated_at)
             VALUES (:d, :c, :u, :co, :b, :g, :ti, :cd, :se, :mo, :y, :p, :sh, :by, :at)
             ON CONFLICT (drive_id) DO UPDATE SET
                 catalog = excluded.catalog, university = excluded.university,
                 course = excluded.course, branch = excluded.branch, grp = excluded.grp,
                 title = excluded.title, code = excluded.code, session = excluded.session,
                 month = excluded.month, year = excluded.year, path = excluded.path,
                 catalog_sha1 = excluded.catalog_sha1, catalog_bytes = excluded.catalog_bytes"""
    before = db.execute("SELECT COUNT(*) AS n FROM papers")[0]["n"]
    for i in range(0, len(pending), chunk):
        db.batch([(sql, p) for p in pending[i:i + chunk]])
    stats.inserted = db.execute("SELECT COUNT(*) AS n FROM papers")[0]["n"] - before
    return stats


# --- work ------------------------------------------------------------------

def claim(db: Db, stage: str, owner: str, limit: int, catalog: str | None = None,
          lease_seconds: int = LEASE_SECONDS) -> list[dict]:
    """Lease up to ``limit`` papers that completed ``stage`` and are ready for
    the next step. Expired leases of crashed jobs are reclaimed here too."""
    t = now()
    rows = db.batch([
        # A paper whose worker died holding it has used an attempt. Without
        # this, a PDF that crashes the process would be retried forever.
        ("""UPDATE papers SET status = 'dead', attempts = attempts + 1, last_error = :err,
                              lease_owner = NULL, lease_until = NULL, updated_at = :t
            WHERE stage = :stage AND status = 'leased' AND lease_until < :t
              AND attempts + 1 >= :max AND (:cat IS NULL OR catalog = :cat)""",
         {"err": LEASE_EXPIRED, "t": t, "stage": stage, "max": MAX_ATTEMPTS, "cat": catalog}),
        ("""
        UPDATE papers SET status = 'leased', lease_owner = :o, lease_until = :until,
                          updated_at = :t,
                          attempts = attempts + (CASE WHEN status = 'leased' THEN 1 ELSE 0 END),
                          last_error = CASE WHEN status = 'leased' THEN :err ELSE last_error END
        WHERE drive_id IN (
            SELECT drive_id FROM papers
            WHERE stage = :stage
              AND status IN ('ready', 'leased')
              AND (lease_until IS NULL OR lease_until < :t)
              AND (:cat IS NULL OR catalog = :cat)
            ORDER BY priority DESC, attempts, updated_at, random()
            LIMIT :n)
        RETURNING *""",
        {"o": owner, "until": t + lease_seconds, "t": t, "stage": stage, "cat": catalog,
         "n": limit, "err": LEASE_EXPIRED})])[1]
    return rows


def renew(db: Db, owner: str, drive_ids: list[str], lease_seconds: int = LEASE_SECONDS) -> int:
    """Extend leases still held by ``owner``. Returns how many are still ours."""
    if not drive_ids:
        return 0
    marks = ", ".join(f":i{n}" for n in range(len(drive_ids)))
    params = {f"i{n}": d for n, d in enumerate(drive_ids)}
    params.update(o=owner, until=now() + lease_seconds)
    rows = db.execute(f"""
        UPDATE papers SET lease_until = :until
        WHERE status = 'leased' AND lease_owner = :o AND drive_id IN ({marks})
        RETURNING drive_id""", params)
    return len(rows)


def _fenced_update(db: Db, drive_id: str, owner: str, from_stage: str, sets: dict,
                   event: dict) -> bool:
    cols = ", ".join(f"{k} = :v_{k}" for k in sets)
    params = {f"v_{k}": v for k, v in sets.items()}
    params.update(d=drive_id, o=owner, fs=from_stage)
    rows = db.batch([
        (f"""UPDATE papers SET {cols}
             WHERE drive_id = :d AND stage = :fs AND status = 'leased' AND lease_owner = :o
             RETURNING drive_id""", params),
        (_EVENT_IF_CHANGED, event),
    ])[0]
    return bool(rows)


def advance(db: Db, paper: dict, owner: str, to_stage: str, status: str = "ready",
            detail: str = "", **results) -> bool:
    """Record a completed step. False means the lease was lost - another job
    owns this paper now - and the caller must discard its work."""
    if to_stage not in STAGES or status not in ("ready", "review", "done"):
        raise ValueError(f"bad transition to {to_stage}/{status}")
    unknown = set(results) - RESULT_FIELDS
    if unknown:
        raise ValueError(f"unknown result fields: {sorted(unknown)}")
    sets = {"stage": to_stage, "status": status, "attempts": 0, "last_error": None,
            "lease_owner": None, "lease_until": None, "updated_at": now(), "priority": 0,
            **results}
    if "review_reasons" in sets and not isinstance(sets["review_reasons"], (str, type(None))):
        sets["review_reasons"] = json.dumps(sets["review_reasons"])
    ok = _fenced_update(db, paper["drive_id"], owner, paper["stage"], sets,
                        _event(paper["drive_id"], owner, paper["stage"], to_stage, status, detail))
    if ok:
        return True
    # 0 rows: either the lease was lost, or an earlier attempt of this very
    # call committed and only its reply was lost.
    row = get(db, paper["drive_id"])
    return bool(row and row["stage"] == to_stage and row["status"] == status
                and all(row.get(k) == sets[k] for k in results))


def fail(db: Db, paper: dict, owner: str, error: str) -> bool:
    """Record a failed attempt: back off and retry, or give up after
    MAX_ATTEMPTS. The paper stays at its stage either way."""
    attempts = int(paper.get("attempts") or 0) + 1
    if attempts >= MAX_ATTEMPTS:
        status, until = "dead", None
    else:
        status, until = "ready", now() + BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS)) - 1]
    sets = {"status": status, "attempts": attempts, "last_error": error[:2000],
            "lease_owner": None, "lease_until": until, "updated_at": now()}
    return _fenced_update(db, paper["drive_id"], owner, paper["stage"], sets,
                          _event(paper["drive_id"], owner, paper["stage"], paper["stage"],
                                 status, error))


def release(db: Db, paper: dict, owner: str) -> bool:
    """Give a lease back untouched (e.g. the job is out of time)."""
    sets = {"status": "ready", "lease_owner": None, "lease_until": None, "updated_at": now()}
    return _fenced_update(db, paper["drive_id"], owner, paper["stage"], sets,
                          _event(paper["drive_id"], owner, paper["stage"], paper["stage"],
                                 "ready", "released"))


def set_status(db: Db, drive_id: str, expect_stage: str, expect_status: str, status: str,
               actor: str, detail: str = "") -> bool:
    """Human actions from the review page (approve, retry a dead paper...).
    Fenced on the state the person was looking at."""
    rows = db.batch([
        ("""UPDATE papers SET status = :s, attempts = CASE WHEN :s = 'ready' THEN 0 ELSE attempts END,
                              lease_owner = NULL, lease_until = NULL, updated_at = :t
            WHERE drive_id = :d AND stage = :fs AND status = :fst RETURNING drive_id""",
         {"s": status, "t": now(), "d": drive_id, "fs": expect_stage, "fst": expect_status}),
        (_EVENT_IF_CHANGED, _event(drive_id, actor, expect_stage, expect_stage, status, detail)),
    ])[0]
    return bool(rows)


def requeue(db: Db, actor: str, reason: str, catalog: str | None = None,
            stages: tuple[str, ...] = ("staged",), statuses: tuple[str, ...] = ("review",),
            drive_ids: list[str] | None = None) -> int:
    """Send papers back to the start to be cleaned again (new rules, or a
    reviewer's "re-clean"). Never touches leased or published papers."""
    if set(stages) & {"published", "live_verified"} or "leased" in statuses:
        raise ValueError("published or leased papers cannot be requeued")
    params: dict = {"t": now(), "c": catalog, "a": actor, "x": reason[:2000]}
    marks_s = ", ".join(f":s{i}" for i in range(len(stages)))
    marks_t = ", ".join(f":u{i}" for i in range(len(statuses)))
    params.update({f"s{i}": v for i, v in enumerate(stages)})
    params.update({f"u{i}": v for i, v in enumerate(statuses)})
    where = f"stage IN ({marks_s}) AND status IN ({marks_t}) AND (:c IS NULL OR catalog = :c)"
    if drive_ids:
        where += " AND drive_id IN (" + ", ".join(f":d{i}" for i in range(len(drive_ids))) + ")"
        params.update({f"d{i}": v for i, v in enumerate(drive_ids)})
    rows = db.batch([
        (f"""INSERT INTO events (drive_id, at, owner, from_stage, to_stage, status, detail)
             SELECT drive_id, :t, :a, stage, 'discovered', 'ready', :x FROM papers
             WHERE {where}""", params),
        (f"""UPDATE papers SET stage = 'discovered', status = 'ready', attempts = 0,
                              last_error = NULL, lease_owner = NULL, lease_until = NULL,
                              updated_at = :t, priority = 1
             WHERE {where} RETURNING drive_id""", params),
    ])[1]
    return len(rows)


def get(db: Db, drive_id: str) -> dict | None:
    rows = db.execute("SELECT * FROM papers WHERE drive_id = :d", {"d": drive_id})
    return rows[0] if rows else None


def summary(db: Db) -> list[dict]:
    return db.execute("""SELECT catalog, stage, status, COUNT(*) AS n FROM papers
                         GROUP BY catalog, stage, status ORDER BY catalog, stage, status""")


def heartbeat(db: Db, name: str) -> None:
    db.execute("INSERT INTO meta (key, value) VALUES (:k, :v) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
               {"k": f"heartbeat:{name}", "v": str(now())})
