"""Publish approved papers: point the live site at the cleaned copies.

For each approved paper, as one unit:

    find the site's rows that link the original Drive file -> share the cleaned
    copy -> confirm it opens for anyone without signing in -> switch the rows'
    drive_link / preview_link to the cleaned copy (fenced on the old values)
    -> record the old values -> mark the paper published

Every site link has the shape https://drive.google.com/file/d/<id>/view (all
41,024 rows in the site DB), so the original link can always be rebuilt from
the original file id: unpublishing never depends on anything but the id.
The original file is never touched or deleted.

After each batch the site itself must still answer, or the run stops.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import state
from .db import Db, DbError
from .drive import DriveClient, DriveError

BATCH = 20
SITE_TIMEOUT = 120        # Render's free tier can take ~1 minute to wake


def view_link(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def preview_link(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/preview"


class PublishStop(RuntimeError):
    """Stop the run, leave remaining papers for the next one."""


@dataclass
class PublishStats:
    published: int = 0
    not_on_site: int = 0
    failed: int = 0
    rows_switched: int = 0
    reasons: dict = field(default_factory=dict)


def opens_publicly(file_id: str) -> bool:
    """True if the cleaned copy opens with no Google login. A private file
    redirects to accounts.google.com, which urllib follows to a 200 - so the
    final address is what tells them apart."""
    req = urllib.request.Request(preview_link(file_id), headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status == 200 and "accounts.google.com" not in resp.geturl()
    except urllib.error.URLError:
        return False


def site_answers(site_url: str) -> bool:
    try:
        with urllib.request.urlopen(site_url, timeout=SITE_TIMEOUT) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _rows_linking(live: Db, file_id: str) -> list[dict]:
    return live.execute(
        "SELECT id, drive_link, preview_link FROM pyq_papers "
        "WHERE instr(drive_link, :p) > 0 OR instr(preview_link, :p) > 0",
        {"p": f"/d/{file_id}/"})


def switch_rows(live: Db, rows: list[dict], new_id: str) -> list[int]:
    """Point rows at ``new_id``; each fenced on the values just read. Returns
    the ids that switched (a row changed in between is left alone)."""
    results = live.batch([
        ("UPDATE pyq_papers SET drive_link = :v, preview_link = :p "
         "WHERE id = :id AND drive_link = :ov AND preview_link = :op RETURNING id",
         {"v": view_link(new_id), "p": preview_link(new_id), "id": r["id"],
          "ov": r["drive_link"], "op": r["preview_link"]})
        for r in rows])
    return [res[0]["id"] for res in results if res]


def restore_rows(live: Db, original_id: str, cleaned_id: str) -> int:
    """Point every row that links the cleaned copy back at the original."""
    rows = live.execute(
        "UPDATE pyq_papers SET drive_link = :v, preview_link = :p "
        "WHERE instr(drive_link, :c) > 0 OR instr(preview_link, :c) > 0 RETURNING id",
        {"v": view_link(original_id), "p": preview_link(original_id),
         "c": f"/d/{cleaned_id}/"})
    return len(rows)


def publish_one(paper: dict, owner: str, state_db: Db, live: Db, drive: DriveClient,
                stats: PublishStats, log, check_public=opens_publicly) -> None:
    orig, new = paper["drive_id"], paper.get("output_file_id")
    if not new:
        raise ValueError("approved paper has no cleaned copy recorded")

    rows = _rows_linking(live, orig)
    if not rows:
        already = _rows_linking(live, new)
        if already:
            # A crash after switching but before recording: finish the record.
            state.advance(state_db, paper, owner, "published", status="done",
                          detail="already switched", published_rows=json.dumps(already))
            stats.published += 1
            return
        state.advance(state_db, paper, owner, "approved", status="done",
                      detail="not on the live site yet")
        stats.not_on_site += 1
        log(f"SKIP   {orig}  not on the live site")
        return

    drive.share_anyone(new)
    if not check_public(new):
        raise ValueError("cleaned copy does not open without a login after sharing")

    switched = switch_rows(live, rows, new)
    if len(switched) != len(rows):
        # Someone edited a row in between: undo ours, let a person look.
        restore_rows(live, orig, new)
        raise ValueError(f"only {len(switched)} of {len(rows)} site rows switched; restored")

    ok = state.advance(state_db, paper, owner, "published", status="done",
                       detail=f"{len(rows)} site row(s)", published_rows=json.dumps(rows))
    if not ok:
        log(f"LOST   {orig}  lease lost after switching; the next run records it")
        return
    stats.published += 1
    stats.rows_switched += len(rows)
    log(f"LIVE   {orig}  -> {new}  ({len(rows)} row)")


def run(state_db: Db, live: Db, drive: DriveClient, site_url: str, limit: int,
        max_minutes: float, owner: str, log=print, check_public=opens_publicly,
        check_site=site_answers) -> PublishStats:
    state.migrate(state_db)
    stats = PublishStats()
    deadline = time.monotonic() + max_minutes * 60
    if not check_site(site_url):
        raise PublishStop(f"site {site_url} does not answer; nothing published")
    done = 0
    while done < limit and time.monotonic() < deadline:
        batch = state.claim(state_db, "approved", owner, min(BATCH, limit - done))
        if not batch:
            break
        done += len(batch)
        for paper in batch:
            try:
                publish_one(paper, owner, state_db, live, drive, stats, log, check_public)
            except (DbError, DriveError) as exc:
                state.release(state_db, paper, owner)
                raise PublishStop(f"{type(exc).__name__}: {exc}") from exc
            except Exception as exc:
                stats.failed += 1
                state.fail(state_db, paper, owner, f"publish: {type(exc).__name__}: {exc}")
                log(f"FAIL   {paper['drive_id']}  {exc}")
        if not check_site(site_url):
            raise PublishStop(f"site {site_url} stopped answering after a batch")
        state.heartbeat(state_db, "publish")
    return stats


def unpublish(state_db: Db, live: Db, drive_ids: list[str], actor: str, reason: str) -> int:
    """Put the original links back for these papers and send them to review."""
    count = 0
    for pid in drive_ids:
        paper = state.get(state_db, pid)
        if not paper or paper["stage"] not in ("published", "live_verified"):
            continue
        count += restore_rows(live, pid, paper["output_file_id"])
        state_db.batch([
            ("UPDATE papers SET stage = 'staged', status = 'review', "
             "review_reasons = :r, updated_at = :t WHERE drive_id = :d",
             {"r": json.dumps(["unpublished"]), "t": state.now(), "d": pid}),
            ("INSERT INTO events (drive_id, at, owner, from_stage, to_stage, status, detail) "
             "VALUES (:d, :t, :a, :f, 'staged', 'review', :x)",
             {"d": pid, "t": state.now(), "a": actor, "f": paper["stage"], "x": reason}),
        ])
    return count
