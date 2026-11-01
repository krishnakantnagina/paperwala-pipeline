"""READ-ONLY: pull the owner's review decisions out of the OLD Turso org, to learn rules from them.

The old cleaning is thrown away (owner, 2026-11-01); nothing here is migrated. The papers that went to review and what the
owner decided about them are the only material worth keeping, as evaluation data for the new pipeline.

    python scripts/old_review_extract.py --out ../local/night/old_review [--wait]

Source databases come from the environment (names only, values never printed):
    PIPELINE_DB_URL / PIPELINE_DB_TOKEN      the old pipeline state DB   (events, papers)
    OLD_SITE_URL / OLD_SITE_TOKEN            the old site DB, optional   (pipeline_review, paper_reports)

Reads are bounded: one row per try while waiting for the org's quota block to lift, then ONE pass over `events` by primary
key (the `owner LIKE` filter has no index, so the pass touches every event once), point lookups for the papers involved, and
whole small tables for the site DB, each with a row cap. PIPELINE_READ_BUDGET (default 2,000,000 here) stops the process.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("PIPELINE_READ_BUDGET", "2000000")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pwpipe.db import METER, DbError, TursoDb  # noqa: E402


def wait_until_readable(db: TursoDb, every: int, give_up_after: int) -> bool:
    started = time.time()
    while True:
        try:
            db.execute("SELECT 1 AS ok FROM sqlite_master LIMIT 1")
            return True
        except DbError as exc:
            if time.time() - started > give_up_after:
                print(f"gave up waiting: {str(exc)[:100]}", flush=True)
                return False
            print(f"not readable yet ({str(exc)[:70]}); retry in {every // 60} min", flush=True)
            time.sleep(every)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--wait", action="store_true")
    ap.add_argument("--every", type=int, default=600)
    ap.add_argument("--give-up-hours", type=float, default=8)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    state = TursoDb(os.environ["PIPELINE_DB_URL"], os.environ["PIPELINE_DB_TOKEN"])
    if args.wait and not wait_until_readable(state, args.every, int(args.give_up_hours * 3600)):
        return 3
    summary: dict = {}

    # 1. the owner's decisions: one pass over events by primary key
    events, last = [], 0
    while True:
        rows = state.execute("SELECT id, drive_id, at, owner, from_stage, to_stage, status, detail FROM events "
                             "WHERE id > :last AND owner LIKE 'admin:%' ORDER BY id LIMIT 500", {"last": last})
        if not rows:
            break
        events += rows
        last = rows[-1]["id"]
        if len(events) % 2000 == 0:
            print(f"events: {len(events)}", flush=True)
        if len(events) >= 200000:
            break
    (out / "events_admin.json").write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    summary["admin_events"] = len(events)

    # 2. the papers they were about (primary-key lookups)
    ids = sorted({e["drive_id"] for e in events})
    papers = []
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        marks = ", ".join(f":p{n}" for n in range(len(chunk)))
        papers += state.execute(
            f"SELECT drive_id, catalog, university, course, title, code, stage, status, review_reasons, auto_ok, report_ref, "
            f"output_file_id, attempts, last_error FROM papers WHERE drive_id IN ({marks})",
            {f"p{n}": d for n, d in enumerate(chunk)})
    (out / "papers_reviewed.json").write_text(json.dumps(papers, ensure_ascii=False), encoding="utf-8")
    summary["reviewed_papers"] = len(papers)

    # 3. the old site DB's review tables (small), if its credentials were given
    if os.environ.get("OLD_SITE_URL") and os.environ.get("OLD_SITE_TOKEN"):
        site = TursoDb(os.environ["OLD_SITE_URL"], os.environ["OLD_SITE_TOKEN"])
        for table, cols in (("pipeline_review", "id, source_id, kind, university, title, reasons, detail, decision, decision_note, decided_by, decided_at, original_link, cleaned_link"),
                            ("paper_reports", "*")):
            try:
                rows = site.execute(f"SELECT {cols} FROM {table} LIMIT 20000")
                (out / f"{table}.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
                summary[table] = len(rows)
            except DbError as exc:
                summary[table] = f"unreadable: {str(exc)[:80]}"
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary), flush=True)
    print(METER.summary(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
