"""Run routing over cleaned papers (see route.py for the decisions).

Progress lives in its own table, ``routing``, so it never disturbs the
cleaning state machine. One job at a time (workflow concurrency group); a
paper that ran out of AI quota is left 'waiting' and retried next run.
"""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import state
from .db import Db
from .keys import QuotaExhausted
from .publish import LinkIndex
from .route import AI, plan_paper, header_text

MAX_ATTEMPTS = 3


@dataclass
class RouteStats:
    checked: int = 0
    confirmed: int = 0
    filled: int = 0
    proposed: int = 0
    not_on_site: int = 0
    waiting_quota: int = 0
    waiting_site: int = 0
    errors: int = 0
    ai_calls: int = 0


def ensure_table(db: Db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS routing (
        drive_id TEXT PRIMARY KEY, status TEXT NOT NULL, result TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL)""")


def _todo(db: Db, limit: int) -> list[dict]:
    return db.execute(
        """SELECT p.* FROM papers p LEFT JOIN routing r ON r.drive_id = p.drive_id
           WHERE p.report_ref IS NOT NULL
             AND p.stage IN ('approved', 'published', 'live_verified')
             AND (r.drive_id IS NULL OR r.status = 'waiting'
                OR (r.status = 'error' AND r.attempts < :m))
           ORDER BY p.updated_at LIMIT :n""", {"m": MAX_ATTEMPTS, "n": limit})


def _record(db: Db, drive_id: str, status: str, result: dict) -> None:
    db.execute(
        """INSERT INTO routing (drive_id, status, result, attempts, updated_at)
           VALUES (:d, :s, :r, :a, :t)
           ON CONFLICT (drive_id) DO UPDATE SET status = excluded.status,
               result = excluded.result, attempts = routing.attempts + excluded.attempts,
               updated_at = excluded.updated_at""",
        # Only errors count against a paper; waiting for quota or for the
        # site's review table can last any number of runs.
        {"d": drive_id, "s": status, "r": json.dumps(result, ensure_ascii=False),
         "a": 1 if status == "error" else 0, "t": state.now()})


def _site(live: Db, paper: dict, index: LinkIndex) -> tuple[dict, dict] | None:
    """The site row (and its subject) for a paper. Found through the link
    index, not ``instr()`` per paper, which read up to 41,024 rows each time."""
    ids = [i for i in (paper["drive_id"], paper.get("output_file_id")) if i]
    for fid in ids:
        linked = index.rows_linking(fid)
        if not linked:
            continue
        rows = live.execute(
            """SELECT p.id AS pid, p.year AS year, p.exam_type AS exam_type, p.title AS title,
                      s.id AS id, s.university AS university, s.program AS program,
                      s.branch AS branch, s.semester AS semester, s.code AS code,
                      s.name AS name, s.aliases AS aliases
               FROM pyq_papers p JOIN subjects s ON s.id = p.subject_id
               WHERE p.id = :i""", {"i": linked[0]["id"]})
        if rows:
            r = rows[0]
            return ({"id": r["pid"], "year": r["year"], "exam_type": r["exam_type"], "title": r["title"]},
                    {k: r[k] for k in ("id", "university", "program", "branch", "semester",
                                       "code", "name", "aliases")})
    return None


class Candidates:
    """Subjects of one university + program, fetched once per run."""

    def __init__(self, live: Db):
        self.live, self.cache = live, {}

    def get(self, university: str, program: str) -> list[dict]:
        key = (university, program)
        if key not in self.cache:
            self.cache[key] = self.live.execute(
                "SELECT id, code, name, aliases, branch, semester FROM subjects "
                "WHERE university = :u AND program = :p", {"u": university, "p": program})
        return self.cache[key]


def apply_fills(live: Db, pyq_id: int, fills: dict) -> int:
    done = 0
    if "exam_type" in fills:
        done += len(live.execute(
            "UPDATE pyq_papers SET exam_type = :v WHERE id = :i AND (exam_type IS NULL OR exam_type = '') "
            "RETURNING id", {"v": fills["exam_type"], "i": pyq_id}))
    if "year" in fills:
        done += len(live.execute(
            "UPDATE pyq_papers SET year = :v WHERE id = :i AND year IS NULL RETURNING id",
            {"v": fills["year"], "i": pyq_id}))
    return done


def propose_move(live: Db, paper: dict, pyq: dict, subject: dict, target: dict, notes: list[str]) -> None:
    detail = {"from_subject_id": subject["id"], "to_subject_id": target["id"],
              "to_subject": target["name"], "notes": " ".join(notes)}
    now = state.now()
    live.execute(
        """INSERT INTO pipeline_review (source_id, pyq_paper_id, university, title, kind, reasons,
               original_link, cleaned_link, crops, detail, decision, synced, created_at, updated_at)
           VALUES (:s, :pp, :u, :t, 'route', :r, '', '', '[]', :d, '', 0,
               datetime(:now, 'unixepoch'), datetime(:now, 'unixepoch'))
           ON CONFLICT (source_id) DO NOTHING""",
        {"s": f"route:{paper['drive_id']}", "pp": pyq["id"], "u": subject["university"],
         "t": (pyq.get("title") or subject["name"])[:255], "r": json.dumps(["move_subject"]),
         "d": json.dumps(detail, ensure_ascii=False), "now": now})


def run(state_db: Db, live: Db, drive, ai: AI | None, limit: int, max_minutes: float,
        log=print) -> RouteStats:
    state.migrate(state_db)
    ensure_table(state_db)
    stats = RouteStats()
    deadline = time.monotonic() + max_minutes * 60
    cands = Candidates(live)
    index = LinkIndex(live)
    workdir = Path(tempfile.mkdtemp(prefix="pwroute-"))
    ai_out = False
    from .sync import tables_ready
    can_propose = tables_ready(live)
    if not can_propose:
        log("site review table not deployed yet: filling details, holding back move proposals")
    for paper in _todo(state_db, limit):
        if time.monotonic() > deadline:
            break
        pid = paper["drive_id"]
        stats.checked += 1
        try:
            site = _site(live, paper, index)
            if site is None:
                stats.not_on_site += 1
                _record(state_db, pid, "done", {"note": "not on the live site"})
                continue
            pyq, subject = site
            rpath = workdir / f"{pid}.json"
            drive.download(paper["report_ref"], rpath)
            header = header_text(json.loads(rpath.read_text(encoding="utf-8")))
            rpath.unlink(missing_ok=True)
            candidates = cands.get(subject["university"], subject["program"])
            try:
                plan = plan_paper(header, pyq, subject, candidates, None if ai_out else ai)
            except QuotaExhausted:
                ai_out = True
                log("AI quota used up for today; continuing with rules only")
                plan = plan_paper(header, pyq, subject, candidates, None)
            if ai_out and not plan.confirmed:
                # Rules alone could not confirm the subject: keep what they
                # found, ask the AI on a later run.
                stats.filled += apply_fills(live, pyq["id"], plan.fills) if plan.fills else 0
                stats.waiting_quota += 1
                _record(state_db, pid, "waiting", {"fills": plan.fills, "note": "needs AI"})
                continue
            stats.ai_calls += int(plan.used_ai)
            filled = apply_fills(live, pyq["id"], plan.fills) if plan.fills else 0
            stats.filled += filled
            if plan.move_to and not can_propose:
                stats.waiting_site += 1
                _record(state_db, pid, "waiting", {"fills": plan.fills, "note": "move held: no review table"})
                continue
            if plan.move_to:
                propose_move(live, paper, pyq, subject, plan.move_to, plan.notes)
                stats.proposed += 1
            stats.confirmed += int(plan.confirmed)
            _record(state_db, pid, "proposed" if plan.move_to else "done",
                    {"fills": plan.fills, "confirmed": plan.confirmed, "notes": plan.notes,
                     "move_to": plan.move_to["id"] if plan.move_to else None})
            log(f"{'MOVE?' if plan.move_to else 'OK   '} {pid}  fills={plan.fills} "
                f"confirmed={plan.confirmed}")
        except Exception as exc:  # one bad paper never stops routing
            stats.errors += 1
            _record(state_db, pid, "error", {"error": f"{type(exc).__name__}: {exc}"[:500]})
            log(f"ERROR {pid}  {type(exc).__name__}: {exc}")
    state.heartbeat(state_db, "route")
    return stats
