"""Run routing over cleaned papers (see route.py for the decisions).

Progress lives in its own table, ``routing``, so it never disturbs the
cleaning state machine. One job at a time (workflow concurrency group); a
paper that ran out of AI quota is left 'waiting' and retried next run.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import placement, state
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
    moved: int = 0              # placement: moved by the router itself (ROUTE_AUTO_MOVE)
    hidden: int = 0             # placement: taken off the site into unsorted_papers (ROUTE_HIDE_UNSORTED)
    reviews: int = 0            # placement: sent to /admin/review
    unverified: int = 0         # placement: nothing in the header to compare, left as it is


@dataclass
class Flags:
    """Placement is opt-in: off until the pilot report has been read.

    ROUTE_PLACEMENT=1       use the header-based placement engine (pwpipe/placement.py)
    ROUTE_AUTO_MOVE=1       apply a `move` decision (one subject the header names exactly) without a person
    ROUTE_HIDE_UNSORTED=1   take a paper that cannot be placed off the public site (kept in unsorted_papers)
    Without the last two, every outcome that would change a page is a proposal in /admin/review."""
    placement: bool = False
    auto_move: bool = False
    hide_unsorted: bool = False

    @classmethod
    def from_env(cls) -> "Flags":
        def on(k):
            return os.environ.get(k, "") == "1"
        return cls(on("ROUTE_PLACEMENT"), on("ROUTE_AUTO_MOVE"), on("ROUTE_HIDE_UNSORTED"))


def ensure_table(db: Db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS routing (
        drive_id TEXT PRIMARY KEY, status TEXT NOT NULL, result TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL)""")


def route_catalog() -> str:
    """The one catalog routing is limited to (env ROUTE_CATALOG), or '' for all of them."""
    return os.environ.get("ROUTE_CATALOG", "").strip()


def parse_shard(text: str | None) -> tuple[int, int]:
    """'2/6' -> (2, 6): worker 2 of 6 (0-based). None or '' -> (0, 0), meaning no sharding."""
    if not text:
        return 0, 0
    i, n = (int(x) for x in text.split("/"))
    if n < 1 or not 0 <= i < n:
        raise ValueError(f"bad shard {text!r}: want i/n with 0 <= i < n")
    return i, n


# Each worker takes the papers whose id lands in its slice, so two workers never
# touch the same paper and no lease is needed. Drive ids end in base64 characters.
SHARD_SQL = "(:sn = 0 OR ((unicode(substr(p.drive_id, -1)) + unicode(substr(p.drive_id, -2, 1))) % :sn) = :si)"


def _todo(db: Db, limit: int, catalog: str | None = None, shard: tuple[int, int] = (0, 0)) -> list[dict]:
    catalog = route_catalog() if catalog is None else catalog
    si, sn = shard
    return db.execute(
        f"""SELECT p.* FROM papers p LEFT JOIN routing r ON r.drive_id = p.drive_id
           WHERE p.report_ref IS NOT NULL
             AND p.stage IN ('approved', 'published', 'live_verified')
             AND (:c = '' OR p.catalog = :c)
             AND {SHARD_SQL}
             AND (r.drive_id IS NULL OR r.status = 'waiting'
                OR (r.status = 'error' AND r.attempts < :m))
           ORDER BY p.updated_at LIMIT :n""",
        {"m": MAX_ATTEMPTS, "n": limit, "c": catalog, "si": si, "sn": sn})


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


class Catalogs:
    """Placement catalogue per university: one read of the subject list per run."""

    def __init__(self, live: Db):
        self.live, self.cache = live, {}

    def get(self, university: str) -> placement.Catalog:
        if university not in self.cache:
            try:
                rows = self.live.execute(
                    "SELECT id, program, branch, semester, code, name, scheme FROM subjects WHERE university = :u",
                    {"u": university})
            except Exception:       # a site that has not run migration 0010 yet has no scheme column
                rows = [dict(r, scheme="") for r in self.live.execute(
                    "SELECT id, program, branch, semester, code, name FROM subjects WHERE university = :u",
                    {"u": university})]
            self.cache[university] = placement.Catalog([
                placement.Subject(r["id"], r["program"] or "", r["branch"] or "", r["semester"] or 0,
                                  r["code"] or "", r["name"] or "", r.get("scheme") or "") for r in rows])
        return self.cache[university]


def unsorted_ready(live: Db) -> bool:
    return bool(live.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'unsorted_papers'"))


def propose_review(live: Db, paper: dict, pyq: dict, subject: dict, reason: str, place, target_name: str = "") -> None:
    """A decision a person must make (or just look at): nothing about the page changes yet."""
    detail = {"from_subject_id": subject["id"], "to_subject_id": place.target_id, "to_subject": target_name,
              "notes": " ".join(place.notes), "placement": place.kind, "reason": place.reason}
    now = state.now()
    live.execute(
        """INSERT INTO pipeline_review (source_id, pyq_paper_id, university, title, kind, reasons,
               original_link, cleaned_link, crops, detail, decision, synced, created_at, updated_at)
           VALUES (:s, :pp, :u, :t, 'route', :r, '', '', '[]', :d, '', 0,
               datetime(:now, 'unixepoch'), datetime(:now, 'unixepoch'))
           ON CONFLICT (source_id) DO NOTHING""",
        {"s": f"route:{paper['drive_id']}", "pp": pyq["id"], "u": subject["university"],
         "t": (pyq.get("title") or subject["name"])[:255], "r": json.dumps([reason]),
         "d": json.dumps(detail, ensure_ascii=False), "now": now})


def hide_paper(live: Db, pyq: dict, subject: dict, place, header: str) -> int:
    """Copy the row into unsorted_papers, then delete it from pyq_papers, in one transaction.
    Fenced on the subject it was seen under, so a row an admin moved meanwhile is left alone."""
    now = state.now()
    live.batch([
        ("""INSERT INTO unsorted_papers (old_paper_id, old_subject_id, old_subject_name, university, program,
                year, exam_type, medium, title, drive_link, preview_link, file_size, reason, detail, header_text, created_at)
            SELECT p.id, s.id, s.name, s.university, s.program, p.year, p.exam_type, p.medium, p.title,
                   p.drive_link, p.preview_link, p.file_size, :reason, :detail, :header, datetime(:now, 'unixepoch')
            FROM pyq_papers p JOIN subjects s ON s.id = p.subject_id
            WHERE p.id = :p AND p.subject_id = :s""",
         {"p": pyq["id"], "s": subject["id"], "reason": place.reason, "detail": " ".join(place.notes)[:300],
          "header": (header or "")[:1500], "now": now}),
        ("DELETE FROM pyq_papers WHERE id = :p AND subject_id = :s AND EXISTS "
         "(SELECT 1 FROM unsorted_papers WHERE old_paper_id = :p)", {"p": pyq["id"], "s": subject["id"]}),
    ])
    return 1


def placement_outcome(live: Db, paper: dict, pyq: dict, subject: dict, header: str, catalogs: Catalogs,
                      cands: "Candidates", flags: Flags, stats: RouteStats, can_hide: bool, can_propose: bool):
    """Route one paper by what its header says. None = nothing to compare, use the old name/AI path."""
    cat = catalogs.get(subject["university"])
    filed = cat.by_id.get(subject["id"])
    if filed is None:
        return None
    place = placement.decide(header, filed, cat, ocr=True)
    if place.kind == "unverified":
        stats.unverified += 1
        return None
    fills = plan_paper(header, pyq, subject, cands.get(subject["university"], subject["program"]), None).fills
    stats.filled += apply_fills(live, pyq["id"], fills) if fills else 0
    result = {"placement": place.kind, "reason": place.reason, "fills": fills, "notes": place.notes}
    kind = place.kind
    target = cat.by_id.get(place.target_id) if place.target_id else None
    if kind == "keep":
        stats.confirmed += 1
        return {**result, "status": "done"}
    if kind == "move" and target is not None and flags.auto_move:
        moved = live.execute("UPDATE pyq_papers SET subject_id = :to WHERE id = :p AND subject_id = :fr RETURNING id",
                             {"to": target.id, "p": pyq["id"], "fr": subject["id"]})
        stats.moved += len(moved)
        return {**result, "status": "done", "moved_to": target.id}
    if kind == "unsorted" and flags.hide_unsorted and can_hide:
        stats.hidden += hide_paper(live, pyq, subject, place, header)
        return {**result, "status": "done", "hidden": True}
    if not can_propose:
        stats.waiting_site += 1
        return {**result, "status": "waiting"}
    reason = {"move": "move_subject", "review": "placement_review", "new_subject": "new_subject",
              "unsorted": "unsorted"}.get(kind, "placement_review")
    propose_review(live, paper, pyq, subject, reason, place, target.name if target else "")
    stats.reviews += 1
    return {**result, "status": "proposed"}


def run(state_db: Db, live: Db, drive, ai: AI | None, limit: int, max_minutes: float,
        log=print, flags: Flags | None = None, shard: tuple[int, int] = (0, 0)) -> RouteStats:
    state.migrate(state_db)
    ensure_table(state_db)
    stats = RouteStats()
    deadline = time.monotonic() + max_minutes * 60
    cands = Candidates(live)
    flags = flags or Flags.from_env()
    catalogs = Catalogs(live)
    can_hide = flags.hide_unsorted and unsorted_ready(live)
    index = LinkIndex(live)
    workdir = Path(tempfile.mkdtemp(prefix="pwroute-"))
    ai_out = False
    from .sync import tables_ready
    can_propose = tables_ready(live)
    if not can_propose:
        log("site review table not deployed yet: filling details, holding back move proposals")
    for paper in _todo(state_db, limit, shard=shard):
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
            if flags.placement:
                res = placement_outcome(live, paper, pyq, subject, header, catalogs, cands, flags, stats,
                                        can_hide, can_propose)
                if res is not None:
                    _record(state_db, pid, res.pop("status"), res)
                    log(f"PLACE {pid}  {res['placement']}/{res['reason']}")
                    continue
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
