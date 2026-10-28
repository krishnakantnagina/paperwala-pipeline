"""Two-way sync with the site's review queue (site migration 0009).

The site holds no pipeline credentials. Instead this job, which already has
the live database for publishing:

1. pushes papers waiting for a person into ``pipeline_review`` (with their
   crops and cleaned copy shared, so /admin/review can show them);
2. pulls the decisions made there and applies them to the pipeline state;
3. pulls decided student reports (``paper_reports``) and acts on them.

Every row it has acted on is marked ``synced``, so a decision is applied
once even if this job runs twice. Before the site migration is deployed the
tables do not exist; the job then reports that and does nothing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import publish, state
from .db import Db, DbError

_ID_IN_LINK = re.compile(r"/d/([A-Za-z0-9_-]{20,})")


@dataclass
class SyncStats:
    pushed: int = 0
    not_on_site: int = 0
    approved: int = 0
    kept_original: int = 0
    recleaned: int = 0
    moved: int = 0
    dropped_stale: int = 0
    reports_restored: int = 0
    reports_recleaned: int = 0
    skipped: list[str] = field(default_factory=list)


def tables_ready(live: Db) -> bool:
    rows = live.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                        "AND name IN ('pipeline_review', 'paper_reports')")
    return len(rows) == 2


def _site_row(live: Db, file_id: str) -> dict | None:
    rows = live.execute(
        "SELECT p.id AS id, s.university AS university, p.title AS title FROM pyq_papers p "
        "JOIN subjects s ON s.id = p.subject_id "
        "WHERE instr(p.drive_link, :p) > 0 OR instr(p.preview_link, :p) > 0 LIMIT 1",
        {"p": f"/d/{file_id}/"})
    return rows[0] if rows else None


# --- 1. push ----------------------------------------------------------------

def push_reviews(state_db: Db, live: Db, drive, stats: SyncStats, limit: int = 500) -> None:
    papers = state_db.execute(
        """SELECT * FROM papers WHERE stage = 'staged' AND status = 'review'
           AND (review_pushed_at IS NULL OR review_pushed_at < updated_at)
           ORDER BY updated_at LIMIT :n""", {"n": limit})
    for paper in papers:
        pid = paper["drive_id"]
        site = _site_row(live, pid)
        if site is None:
            # Not on the live site: nothing could be published, so nothing
            # to decide yet. Marked so it is not looked up every run.
            stats.not_on_site += 1
            _mark_pushed(state_db, pid)
            continue
        crops = []
        if paper.get("crops_ref"):
            for f in drive.list_folder(paper["crops_ref"]):
                drive.share_anyone(f["id"])
                crops.append({"id": f["id"], "name": f["name"]})
        cleaned = ""
        if paper.get("output_file_id"):
            drive.share_anyone(paper["output_file_id"])
            cleaned = publish.view_link(paper["output_file_id"])
        reasons = [r for r in json.loads(paper.get("review_reasons") or "[]") if r != "pilot"]
        now = state.now()
        live.execute(
            """INSERT INTO pipeline_review (source_id, pyq_paper_id, university, title, kind,
                   reasons, original_link, cleaned_link, crops, decision, synced,
                   created_at, updated_at)
               VALUES (:s, :pp, :u, :t, 'clean', :r, :o, :c, :cr, '', 0,
                   datetime(:now, 'unixepoch'), datetime(:now, 'unixepoch'))
               ON CONFLICT (source_id) DO UPDATE SET
                   pyq_paper_id = excluded.pyq_paper_id, university = excluded.university,
                   title = excluded.title, reasons = excluded.reasons,
                   cleaned_link = excluded.cleaned_link, crops = excluded.crops,
                   decision = '', decision_note = '', decided_by = '', decided_at = NULL,
                   synced = 0, updated_at = excluded.updated_at""",
            {"s": pid, "pp": site["id"], "u": site["university"],
             "t": (site["title"] or paper.get("title") or "")[:255], "r": json.dumps(reasons),
             "o": publish.view_link(pid), "c": cleaned, "cr": json.dumps(crops), "now": now})
        _mark_pushed(state_db, pid)
        stats.pushed += 1


def _mark_pushed(state_db: Db, drive_id: str) -> None:
    # updated_at is left alone: it says when the paper last changed.
    state_db.execute("UPDATE papers SET review_pushed_at = :t WHERE drive_id = :d",
                     {"t": state.now() + 1, "d": drive_id})


def drop_stale_reviews(state_db: Db, live: Db, stats: SyncStats) -> None:
    """Remove undecided queue entries whose paper is no longer waiting for a
    person (re-cleaned since, approved elsewhere): a reviewer must never be
    shown an outdated version."""
    open_rows = live.execute("SELECT id, source_id FROM pipeline_review "
                             "WHERE decision = '' AND kind = 'clean'")
    for i in range(0, len(open_rows), 200):
        chunk = open_rows[i:i + 200]
        marks = ", ".join(f":s{n}" for n in range(len(chunk)))
        waiting = {r["drive_id"] for r in state_db.execute(
            f"SELECT drive_id FROM papers WHERE stage = 'staged' AND status = 'review' "
            f"AND drive_id IN ({marks})", {f"s{n}": r["source_id"] for n, r in enumerate(chunk)})}
        for r in chunk:
            if r["source_id"] not in waiting:
                stats.dropped_stale += len(live.execute(
                    "DELETE FROM pipeline_review WHERE id = :i AND decision = '' RETURNING id",
                    {"i": r["id"]}))


# --- 2. decisions on cleaned papers ------------------------------------------

def pull_decisions(state_db: Db, live: Db, stats: SyncStats) -> None:
    rows = live.execute("SELECT id, source_id, kind, pyq_paper_id, detail, decision, "
                        "decision_note, decided_by FROM pipeline_review "
                        "WHERE decision != '' AND synced = 0")
    for row in rows:
        pid, who = row["source_id"], f"admin:{row['decided_by']}"
        note = row["decision_note"] or ""
        if row["kind"] == "route":
            if row["decision"] == "approve":
                stats.moved += _apply_move(live, row)
        elif row["decision"] == "approve":
            if state.decide(state_db, pid, ("staged", "review"), ("approved", "ready"), who,
                            f"approved in review. {note}".strip()):
                stats.approved += 1
        elif row["decision"] == "keep_original":
            if state.decide(state_db, pid, ("staged", "review"), ("staged", "done"), who,
                            f"kept original in review. {note}".strip()):
                stats.kept_original += 1
        elif row["decision"] == "reclean":
            stats.recleaned += state.requeue(state_db, who, f"re-clean from review. {note}".strip(),
                                             drive_ids=[pid])
        else:
            stats.skipped.append(f"review {row['id']}: unknown decision {row['decision']!r}")
        live.execute("UPDATE pipeline_review SET synced = 1 WHERE id = :i", {"i": row["id"]})


def _apply_move(live: Db, row: dict) -> int:
    """Move a paper to the subject a person approved, fenced on the subject
    it was in when proposed (an admin may have moved it by hand since)."""
    detail = json.loads(row["detail"] or "{}") if isinstance(row["detail"], str) else row["detail"] or {}
    if not detail.get("to_subject_id") or not row["pyq_paper_id"]:
        return 0
    moved = live.execute(
        "UPDATE pyq_papers SET subject_id = :to WHERE id = :p AND subject_id = :fr RETURNING id",
        {"to": detail["to_subject_id"], "p": row["pyq_paper_id"], "fr": detail["from_subject_id"]})
    return len(moved)


# --- 3. student reports --------------------------------------------------------

def _paper_for_site_link(state_db: Db, link: str) -> dict | None:
    m = _ID_IN_LINK.search(link or "")
    if not m:
        return None
    fid = m.group(1)
    rows = state_db.execute("SELECT * FROM papers WHERE drive_id = :f OR output_file_id = :f",
                            {"f": fid})
    return rows[0] if rows else None


def pull_reports(state_db: Db, live: Db, stats: SyncStats) -> None:
    rows = live.execute(
        """SELECT r.id AS id, r.decision AS decision, r.decided_by AS decided_by,
                  p.drive_link AS drive_link
           FROM paper_reports r JOIN pyq_papers p ON p.id = r.pyq_paper_id
           WHERE r.decision != '' AND r.synced = 0""")
    index = publish.LinkIndex(live)    # one scan of pyq_papers, and only if needed
    for row in rows:
        who = f"admin:{row['decided_by']}"
        paper = _paper_for_site_link(state_db, row["drive_link"])
        if row["decision"] != "dismiss" and paper is not None:
            pid = paper["drive_id"]
            live_now = paper["stage"] in ("published", "live_verified")
            if row["decision"] == "restore_original" and live_now:
                publish.unpublish(state_db, live, [pid], who, "student report: restore original",
                                  index)
                state.decide(state_db, pid, ("staged", "review"), ("staged", "done"), who,
                             "kept original after a student report")
                stats.reports_restored += 1
            elif row["decision"] == "reclean":
                if live_now:
                    publish.unpublish(state_db, live, [pid], who, "student report: clean again", index)
                stats.reports_recleaned += state.requeue(
                    state_db, who, "student report: clean again", drive_ids=[pid],
                    stages=("staged", "approved"), statuses=("review", "ready", "done"))
        live.execute("UPDATE paper_reports SET synced = 1 WHERE id = :i", {"i": row["id"]})


def run(state_db: Db, live: Db, drive, log=print) -> SyncStats:
    state.migrate(state_db)
    stats = SyncStats()
    try:
        ready = tables_ready(live)
    except DbError as exc:
        raise RuntimeError(f"live database unreachable: {exc}") from exc
    if not ready:
        log("site review tables not deployed yet (migration 0009); nothing synced")
        return stats
    pull_decisions(state_db, live, stats)
    pull_reports(state_db, live, stats)
    drop_stale_reviews(state_db, live, stats)
    push_reviews(state_db, live, drive, stats)
    return stats
