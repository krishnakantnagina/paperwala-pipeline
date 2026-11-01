"""Keep the earlier cleaned copy of papers that are about to be cleaned again, then requeue them.

    python scripts/archive_previous_clean.py --catalog rgpvonline.com [--dry-run]

Needs PIPELINE_DB_URL/TOKEN and Drive credentials (GDRIVE_OAUTH_JSON or ~/.paperwala/gdrive_oauth.json).

Why: the uploader deletes every other Drive file that carries the same ``pw_source`` once a re-cleaned
copy is verified (drive.py), so a re-clean would destroy the version being replaced. For each cleaned
paper of the catalog this script
  1. downloads the earlier cleaned PDF and counts the pages where a grey diagonal watermark is still
     visible (watermark.grey_stamp_mask) - those are the ones the new rules should fix;
  2. uploads it to Drive folder ``PYQ-previous-clean/<catalog>`` under a different ``pw_source``
     (``prev-<drive_id>-<old file id>``), so the uploader never touches it;
  3. records it in the state table ``previous_cleans`` (old file id, hashes, version, review reasons,
     grey pages, the archive file id);
and only when every paper is archived requeues them (state.requeue), so nothing is lost on a failure.
Papers already in ``previous_cleans`` are not archived twice.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from pwpipe import state  # noqa: E402
from pwpipe.db import METER, TursoDb  # noqa: E402
from pwpipe.drive import DriveClient  # noqa: E402
from pwpipe.pdf import analyze, open_pdf, raster_page  # noqa: E402
from pwpipe.watermark import grey_stamp_mask  # noqa: E402

ARCHIVE_ROOT = "PYQ-previous-clean"

TABLE = """CREATE TABLE IF NOT EXISTS previous_cleans (
    drive_id TEXT NOT NULL, old_output_file_id TEXT NOT NULL, old_output_sha256 TEXT,
    old_pipeline_version TEXT, old_report_ref TEXT, old_review_reasons TEXT, old_stage TEXT,
    grey_pages INTEGER, page_count INTEGER, archive_file_id TEXT, saved_at INTEGER NOT NULL,
    PRIMARY KEY (drive_id, old_output_file_id)
)"""


def grey_pages(path: Path) -> tuple[int, int]:
    """(pages with a grey diagonal watermark still visible, pages) of a cleaned PDF."""
    info = analyze(path)
    doc = open_pdf(path)
    found = 0
    try:
        for page in info.pages:
            raster = raster_page(doc, page, force_render=True)
            if grey_stamp_mask(np.asarray(raster.image.convert("L"))) is not None:
                found += 1
    finally:
        doc.close()
    return found, info.page_count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = TursoDb.from_env()
    db.execute(TABLE)
    rows = db.execute(
        "SELECT drive_id, output_file_id, output_sha256, pipeline_version, report_ref, review_reasons, "
        "stage FROM papers WHERE catalog = :c AND stage IN ('approved', 'staged') "
        "AND status IN ('ready', 'review') AND output_file_id IS NOT NULL", {"c": args.catalog})
    done = {(r["drive_id"], r["old_output_file_id"])
            for r in db.execute("SELECT drive_id, old_output_file_id FROM previous_cleans")}
    todo = [r for r in rows if (r["drive_id"], r["output_file_id"]) not in done]
    print(f"{len(rows)} cleaned papers in {args.catalog}; {len(todo)} not archived yet")
    if args.dry_run:
        return 0

    drive = DriveClient.from_env()
    folder = drive.ensure_folder([ARCHIVE_ROOT, args.catalog])
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for i, r in enumerate(todo, 1):
            try:
                path = Path(tmp) / f"{r['drive_id']}.pdf"
                sha1, _ = drive.download(r["output_file_id"], path)
                grey, pages = grey_pages(path)
                import hashlib
                sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
                up = drive.upload(path, f"{r['drive_id']}__{r['pipeline_version'] or 'old'}.pdf", folder,
                                  f"prev-{r['drive_id']}-{r['output_file_id']}", sha256,
                                  anyone_reader=False)
                db.execute(
                    "INSERT OR REPLACE INTO previous_cleans VALUES (:d, :o, :s, :v, :r, :w, :g, :gp, :pc, "
                    ":a, :t)",
                    {"d": r["drive_id"], "o": r["output_file_id"], "s": r["output_sha256"],
                     "v": r["pipeline_version"], "r": r["report_ref"], "w": r["review_reasons"],
                     "g": r["stage"], "gp": grey, "pc": pages, "a": up.file_id, "t": state.now()})
                if i % 10 == 0:
                    print(f"  archived {i}/{len(todo)}", flush=True)
            except Exception as exc:
                failed += 1
                print(f"  FAILED {r['drive_id']}: {exc}", flush=True)
    if failed:
        print(f"{failed} failed: nothing requeued. Fix and rerun (finished ones are skipped).")
        return 1

    ids = [r["drive_id"] for r in rows]
    n = state.requeue(db, "archive_previous_clean",
                      "re-clean with grey-watermark remover, tiled marks and URL fixes (earlier copy kept "
                      f"in Drive {ARCHIVE_ROOT}/{args.catalog})",
                      stages=("approved", "staged"), statuses=("ready", "review"), drive_ids=ids)
    print(f"requeued {n} papers")
    print("with grey watermark still visible:",
          db.execute("SELECT count(*) AS n FROM previous_cleans WHERE grey_pages > 0")[0]["n"])
    print(METER.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
