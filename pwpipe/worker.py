"""The job a GitHub Actions runner executes: clean papers until time runs out.

For each claimed paper, as one unit:

    download original -> check catalog sha1 -> clean + verify -> upload the
    cleaned PDF (private) into its final folder -> upload report + crops ->
    record the result

The unit is redone from scratch if anything fails part-way: downloads and
cleaning are deterministic, uploads are idempotent (drive.upload reuses a copy
tagged with the same source and hash), and the state write is fenced. So a
crash at any point costs only that paper's time.

Failures are split in two:
- *paper* failures (bad PDF, hash mismatch, OCR crash) count an attempt
  against that paper and the run continues;
- *infrastructure* failures (Drive login revoked, Turso unreachable) stop the
  run, so 50 papers are not burned through 5 attempts each by one outage.
"""

from __future__ import annotations

import json
import random
import shutil
import socket
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, state
from .blocklist import Blocklist
from .db import Db, DbError
from .drive import DriveClient, DriveError
from .process import process_paper

CLAIM_BATCH = 3
RENEW_EVERY = 5 * 60
OUTPUT_ROOT = "PYQ"                 # cleaned papers, mirroring the catalog's folders
PIPELINE_ROOT = "PYQ-pipeline"      # reports and crops for review
AUTO_SAMPLE_RATE = 0.02             # share of auto_ok papers still shown to a person


class InfraError(RuntimeError):
    """Not the paper's fault: stop the run, leave the papers for later."""


@dataclass
class RunStats:
    claimed: int = 0
    approved: int = 0
    review: int = 0
    failed: int = 0
    lost: int = 0
    released: int = 0
    reasons: dict = field(default_factory=dict)


class LeaseKeeper:
    """Renews this worker's leases in the background while a paper is slow."""

    def __init__(self, db: Db, owner: str):
        self.db, self.owner = db, owner
        self.ids: set[str] = set()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=10)

    def hold(self, ids):
        with self._lock:
            self.ids.update(ids)

    def drop(self, drive_id):
        with self._lock:
            self.ids.discard(drive_id)

    def _run(self):
        while not self._stop.wait(RENEW_EVERY):
            with self._lock:
                ids = list(self.ids)
            try:
                state.renew(self.db, self.owner, ids)
            except Exception:
                pass  # next tick retries; an unrenewed lease only means a later reclaim


def _infra(exc: Exception) -> bool:
    if isinstance(exc, DbError):
        return True
    if isinstance(exc, DriveError):
        msg = str(exc)
        if "token refresh" in msg or exc.status in (None, 401, 500, 502, 503, 504):
            return True     # login gone, network down, or Drive down after retries
        if exc.status == 403:
            # Account-wide limits stop the run. Any other 403 is about one
            # file (not shared, its own download quota) and must only fail
            # that paper - otherwise one bad file would stall every run.
            return any(k in msg for k in ("storageQuotaExceeded", "dailyLimitExceeded",
                                          "quotaExceeded\"", "teamDriveFileLimitExceeded"))
        return False
    return False


class FolderCache:
    def __init__(self, drive: DriveClient):
        self.drive, self.ids = drive, {}

    def get(self, parts: list[str]) -> str:
        key = tuple(parts)
        if key not in self.ids:
            self.ids[key] = self.drive.ensure_folder(list(parts))
        return self.ids[key]


def _output_location(paper: dict) -> tuple[list[str], str]:
    """Folder parts and file name for the cleaned copy, from the catalog path
    ("MP Board/Class 12/Trimasik/Quarterly Biology - 2027.pdf")."""
    parts = [p for p in (paper.get("path") or "").replace("\\", "/").split("/") if p]
    if len(parts) >= 2:
        return [OUTPUT_ROOT, *parts[:-1]], parts[-1]
    return [OUTPUT_ROOT, paper["catalog"]], f"{paper['drive_id']}.pdf"


def handle_paper(paper: dict, drive: DriveClient, folders: FolderCache, bl: Blocklist,
                 workdir: Path, review_all: bool) -> dict:
    """Do the whole unit for one paper; return the fields to record."""
    pdir = workdir / paper["drive_id"]
    shutil.rmtree(pdir, ignore_errors=True)
    pdir.mkdir(parents=True)
    try:
        src = pdir / "original.pdf"
        sha1, size = drive.download(paper["drive_id"], src)
        expected = paper.get("catalog_sha1")
        if expected and sha1 != expected:
            raise ValueError(f"original changed on Drive: sha1 {sha1} != catalog {expected}")

        out = pdir / "clean.pdf"
        report = process_paper(src, out, bl, paper_id=paper["drive_id"], crops_dir=pdir / "crops")

        reasons = list(report["review"])
        if review_all:
            reasons.append("pilot")
        elif not reasons and random.random() < AUTO_SAMPLE_RATE:
            reasons.append("sample")

        parts, name = _output_location(paper)
        uploaded = drive.upload(out, name, folders.get(parts), paper["drive_id"],
                                report["output_sha256"], anyone_reader=False)

        rdir = folders.get([PIPELINE_ROOT, "reports", paper["catalog"]])
        rpath = pdir / f"{paper['drive_id']}.json"
        rpath.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        rep = drive.upload(rpath, rpath.name, rdir, f"report:{paper['drive_id']}",
                           report["output_sha256"], anyone_reader=False,
                           mime="application/json")

        crops_ref = None
        crop_files = sorted((pdir / "crops").glob("*.png")) if (pdir / "crops").exists() else []
        if crop_files and reasons:
            cdir = folders.get([PIPELINE_ROOT, "crops", paper["catalog"], paper["drive_id"]])
            for f in crop_files:
                drive.upload(f, f.name, cdir, f"crop:{paper['drive_id']}:{f.name}",
                             report["output_sha256"], anyone_reader=False, mime="image/png")
            crops_ref = cdir

        return {
            "reasons": reasons,
            "fields": {
                "source_sha256": report["source_sha256"],
                "output_sha256": report["output_sha256"],
                "pipeline_version": __version__,
                "blocklist_version": bl.version,
                "auto_ok": int(report["auto_ok"]),
                "review_reasons": json.dumps(reasons),
                "report_ref": rep.file_id,
                "output_file_id": uploaded.file_id,
                "output_bytes": uploaded.size,
                "crops_ref": crops_ref,
            },
        }
    finally:
        shutil.rmtree(pdir, ignore_errors=True)


def run(db: Db, drive: DriveClient, bl: Blocklist, catalog: str | None, limit: int,
        max_minutes: float, owner: str | None = None, review_all: bool = False,
        log=print) -> RunStats:
    owner = owner or f"{socket.gethostname()}-{int(time.time())}"
    deadline = time.monotonic() + max_minutes * 60
    stats = RunStats()
    state.migrate(db)
    folders = FolderCache(drive)
    workdir = Path(tempfile.mkdtemp(prefix="pwpipe-"))
    # Budget for one paper: OCR + verify of a long scan can take minutes.
    per_paper_reserve = 6 * 60

    try:
        with LeaseKeeper(db, owner) as keeper:
            while stats.claimed < limit and time.monotonic() < deadline - per_paper_reserve:
                batch = state.claim(db, "discovered", owner,
                                    min(CLAIM_BATCH, limit - stats.claimed), catalog)
                if not batch:
                    break
                stats.claimed += len(batch)
                keeper.hold(p["drive_id"] for p in batch)
                for i, paper in enumerate(batch):
                    if time.monotonic() > deadline - per_paper_reserve:
                        for rest in batch[i:]:
                            if state.release(db, rest, owner):
                                stats.released += 1
                            keeper.drop(rest["drive_id"])
                        break
                    _one(db, drive, folders, bl, workdir, paper, owner, review_all, stats, log)
                    keeper.drop(paper["drive_id"])
                state.heartbeat(db, "worker")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return stats


def _one(db, drive, folders, bl, workdir, paper, owner, review_all, stats, log):
    pid = paper["drive_id"]
    started = time.monotonic()
    try:
        result = handle_paper(paper, drive, folders, bl, workdir, review_all)
    except Exception as exc:
        if _infra(exc):
            # Give the paper back untouched and stop: the outage is not its fault.
            state.release(db, paper, owner)
            raise InfraError(f"{type(exc).__name__}: {exc}") from exc
        stats.failed += 1
        detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}"
        state.fail(db, paper, owner, detail)
        log(f"FAIL   {pid}  {type(exc).__name__}: {str(exc)[:120]}")
        return

    reasons = result["reasons"]
    if reasons:
        ok = state.advance(db, paper, owner, "staged", status="review", **result["fields"])
        bucket = "review"
    else:
        ok = state.advance(db, paper, owner, "approved", status="ready", **result["fields"])
        bucket = "approved"
    if not ok:
        stats.lost += 1
        log(f"LOST   {pid}  lease taken over; result discarded")
        return
    setattr(stats, bucket, getattr(stats, bucket) + 1)
    for r in reasons:
        stats.reasons[r] = stats.reasons.get(r, 0) + 1
    log(f"{bucket.upper():<6} {pid}  {time.monotonic() - started:5.0f}s  {','.join(reasons)}")
