"""Should a job start its next run straight away?

GitHub's scheduler is best-effort: on this repo it fired nothing at all for
the first hours after the schedules were added. So every workflow ends by
asking two questions - is any of my work left, and did this run move any of
it? - and starts its own next run when both are yes (the workflow does the
starting, with ``scripts/start_run.sh``). The schedules stay as a second net.

Progress is the loop guard. A run that moved nothing (Drive down, AI quota
gone, a bug that fails every paper) does not start another, so a fault can
never spin up runs forever; the schedule and the daily health check take it
from there.
"""

from __future__ import annotations

from .db import Db

JOBS = ("work", "publish", "route")


CAP = 1000


def _count(db: Db, sql: str, params: dict | None = None) -> int:
    """How many rows ``sql`` (a plain ``SELECT 1 FROM ...``) matches, up to
    CAP. Callers only ask "any left?" and "did this run move any?", so a count
    that stops at CAP reads CAP rows at most, where COUNT(*) read every match
    (every paper still waiting, every event of the run) on each call."""
    return int(db.execute(f"SELECT COUNT(*) AS n FROM ({sql} LIMIT {CAP})", params or {})[0]["n"])


def left_and_moved(db: Db, job: str, since: int, owner: str) -> tuple[int, int]:
    """Papers still waiting for ``job``, and papers this run moved since
    ``since``. ``owner`` is the run's lease owner, or its prefix for a run
    with several workers ("gha-123-" covers gha-123-1 ... gha-123-16)."""
    p = {"t": since, "o": f"{owner}%"}
    if job == "work":
        return (_count(db, """SELECT 1 FROM papers
                              WHERE stage = 'discovered' AND status IN ('ready', 'leased')"""),
                _count(db, """SELECT 1 FROM events
                              WHERE at >= :t AND owner LIKE :o
                                AND to_stage IN ('staged', 'approved')""", p))
    if job == "publish":
        return (_count(db, """SELECT 1 FROM papers
                              WHERE stage = 'approved' AND status IN ('ready', 'leased')"""),
                _count(db, """SELECT 1 FROM events
                              WHERE at >= :t AND owner LIKE :o AND status = 'done'""", p))
    if job == "route":
        from .route_job import MAX_ATTEMPTS, ensure_table
        ensure_table(db)
        # Same filter as route_job._todo. 'waiting' rows (no AI quota) are
        # work left, but re-checking them without quota is not progress.
        return (_count(db, """SELECT 1 FROM papers p
                              LEFT JOIN routing r ON r.drive_id = p.drive_id
                              WHERE p.report_ref IS NOT NULL
                                AND p.stage IN ('approved', 'published', 'live_verified')
                                AND (r.drive_id IS NULL OR r.status = 'waiting'
                                     OR (r.status = 'error' AND r.attempts < :m))""",
                       {"m": MAX_ATTEMPTS}),
                _count(db, """SELECT 1 FROM routing
                              WHERE updated_at >= :t AND status IN ('done', 'proposed')""", p))
    raise ValueError(f"unknown job {job!r}; expected one of {JOBS}")


def again(db: Db, job: str, since: int, owner: str) -> tuple[bool, str]:
    left, moved = left_and_moved(db, job, since, owner)
    if not left:
        return False, f"{job}: nothing left"
    if not moved:
        return False, f"{job}: {left} left but this run moved none - not looping on a fault"
    return True, f"{job}: {left} left, this run moved {moved}"
