"""Put the original links back for papers of every catalog except one, and hold them as approved.

    LIVE_DB_URL/TOKEN, PIPELINE_DB_URL/TOKEN set
    python scripts/revert_published.py --keep rgpvonline.com [--dry-run]

Why: the publish job takes any approved paper, whatever its catalog (2026-11-02 it published UP Board,
MP Board and DAVV papers cleaned by an older pipeline, before their own placement and review). For each such
paper this restores the original links on the live site (publish.restore_rows: only rows that still link
the cleaned copy are touched) and sets the paper back to stage approved, status review, reason
hold_other_category - held, so nothing publishes it until its catalog is worked on.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pwpipe import state  # noqa: E402
from pwpipe.db import METER, connect, connect_live  # noqa: E402
from pwpipe.publish import LinkIndex, restore_rows  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", required=True, help="catalog whose published papers stay published")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    st = connect(None)
    live = connect_live()
    rows = st.execute(
        "SELECT drive_id, output_file_id, catalog FROM papers WHERE stage IN ('published', 'live_verified') "
        "AND catalog <> :k", {"k": args.keep})
    print(f"{len(rows)} published papers outside {args.keep}", flush=True)
    if args.dry_run or not rows:
        return 0
    index = LinkIndex(live)
    restored = held = 0
    for i, r in enumerate(rows, 1):
        try:
            restored += restore_rows(live, r["drive_id"], r["output_file_id"], index)
            st.batch([
                ("UPDATE papers SET stage = 'approved', status = 'review', review_reasons = :r, "
                 "lease_owner = NULL, lease_until = NULL, updated_at = :t WHERE drive_id = :d "
                 "AND stage IN ('published', 'live_verified')",
                 {"r": json.dumps(["hold_other_category"]), "t": state.now(), "d": r["drive_id"]}),
                ("INSERT INTO events (drive_id, at, owner, from_stage, to_stage, status, detail) "
                 "VALUES (:d, :t, 'claude-revert', 'published', 'approved', 'review', :x)",
                 {"d": r["drive_id"], "t": state.now(),
                  "x": "published by mistake before its catalog was worked on; original links restored"}),
            ])
            held += 1
        except Exception as exc:
            print(f"  FAILED {r['drive_id']}: {type(exc).__name__}: {exc}", flush=True)
        if i % 100 == 0:
            print(f"  {i}/{len(rows)} (site rows restored {restored})", flush=True)
    print(f"held {held} papers, restored {restored} site rows")
    print(METER.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
