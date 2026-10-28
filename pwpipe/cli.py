"""Local command line, for the pilot and for debugging one paper.

    python -m pwpipe process IN.pdf OUT.pdf [--report r.json] [--crops DIR]
    python -m pwpipe discover REPORT.json ... --min-files 20 --out candidates.csv
    python -m pwpipe approve candidates.csv [--blocklist seed.json]
    python -m pwpipe migrate [--db turso|PATH]
    python -m pwpipe seed CATALOG_DIR --catalog NAME [--db turso|PATH]
    python -m pwpipe status [--db turso|PATH]
    python -m pwpipe work [--catalog NAME] [--limit N] [--max-minutes M] [--review-all]
    python -m pwpipe review-report [--catalog NAME] [--out DIR]
    python -m pwpipe requeue --reason TEXT [--catalog NAME] [--stage S ...] [--status S ...]
    python -m pwpipe health          (exit 1 = something needs a person)
    python -m pwpipe publish [--limit N] [--max-minutes M]
    python -m pwpipe unpublish DRIVE_ID ... --reason TEXT
    python -m pwpipe sync            (site review queue <-> pipeline, both ways)
    python -m pwpipe route [--limit N] [--max-minutes M]

``--db`` defaults to Turso via PIPELINE_DB_URL / PIPELINE_DB_TOKEN.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import state
from .blocklist import SEED_PATH, Blocklist
from .db import connect
from .discover import aggregate, apply_approved, fingerprint, write_candidates
from .process import process_paper


def _process(args) -> int:
    bl = Blocklist.load(args.blocklist)
    report = process_paper(args.src, args.dst, bl, crops_dir=args.crops)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    summary = {k: report[k] for k in ("paper", "changed", "pages_dropped", "review", "auto_ok")}
    summary["verify"] = {k: report["verify"][k] for k in ("ok", "failures")}
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if report["verify"]["ok"] else 2


def _discover(args) -> int:
    bl = Blocklist.load(args.blocklist)
    fps = [fingerprint(json.loads(Path(p).read_text(encoding="utf-8"))) for p in args.reports]
    candidates = aggregate(fps, bl, args.min_files)
    write_candidates(candidates, Path(args.out))
    print(f"{len(candidates)} candidates from {len(fps)} papers -> {args.out}")
    return 0


def _approve(args) -> int:
    bl = apply_approved(Blocklist.load(args.blocklist), Path(args.candidates))
    bl.save(args.blocklist)
    print(f"blocklist {args.blocklist} now version {bl.version}")
    return 0


def _migrate(args) -> int:
    db = connect(args.db)
    state.migrate(db)
    print(f"schema at version {state.SCHEMA_VERSION}")
    return 0


def _seed(args) -> int:
    db = connect(args.db)
    state.migrate(db)
    stats = state.seed_catalog(db, args.catalog_dir, args.catalog)
    print(f"{args.catalog}: {stats.rows} rows, {stats.inserted} new papers, "
          f"{stats.no_drive_id} without a Drive id (skipped)")
    return 0


def _status(args) -> int:
    db = connect(args.db)
    rows = state.summary(db)
    width = max([len(r["catalog"]) for r in rows] + [7])
    for r in rows:
        print(f"{r['catalog']:<{width}}  {r['stage']:<14} {r['status']:<7} {r['n']:>6}")
    print(f"{'total':<{width}}  {sum(r['n'] for r in rows):>29}")
    return 0


def _work(args) -> int:
    import os
    from dataclasses import asdict

    from .drive import DriveClient
    from .worker import InfraError, run

    db = connect(args.db)
    drive = DriveClient.from_env()
    bl = Blocklist.load(args.blocklist)
    try:
        # A scheduled run passes an empty catalog: that means every catalog.
        stats = run(db, drive, bl, args.catalog or None, args.limit, args.max_minutes,
                    owner=args.owner, review_all=args.review_all,
                    log=lambda m: print(m, flush=True))
        code = 0
    except InfraError as exc:
        print(f"STOPPED: infrastructure problem, papers left for the next run: {exc}", flush=True)
        stats, code = None, 3
    summary = json.dumps(asdict(stats) if stats else {"stopped": True}, indent=2)
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"### worker {args.owner}\n```json\n{summary}\n```\n")
    return code


def _review_report(args) -> int:
    from .drive import DriveClient
    from .review_report import build

    index = build(connect(args.db), DriveClient.from_env(), Path(args.out), args.catalog)
    print(f"review page: {index.resolve()}")
    return 0


def _requeue(args) -> int:
    n = state.requeue(connect(args.db), "cli", args.reason, args.catalog,
                      tuple(args.stage), tuple(args.status), args.ids or None)
    print(f"requeued {n} papers")
    return 0


def _health(args) -> int:
    from .drive import DriveClient
    from .health import Health, check_drive, check_state, report

    h = Health()
    try:
        check_state(connect(args.db), h)
    except Exception as exc:
        h.problems.append(f"state database unreachable: {exc}")
    try:
        check_drive(DriveClient.from_env(), h)
    except Exception as exc:
        h.problems.append(f"Google Drive credentials missing or unreadable: {exc}")
    print(report(h))
    return 0 if h.ok else 1


def _publish(args) -> int:
    import os
    from dataclasses import asdict

    from .db import connect_live
    from .drive import DriveClient
    from .publish import PublishStop, run

    site = os.environ.get("SITE_URL", "https://paperwala-b9nn.onrender.com")
    try:
        stats = run(connect(args.db), connect_live(), DriveClient.from_env(), site,
                    args.limit, args.max_minutes, owner=args.owner or "publish",
                    log=lambda m: print(m, flush=True))
    except PublishStop as exc:
        print(f"STOPPED: {exc}", flush=True)
        return 3
    print(json.dumps(asdict(stats), indent=2))
    return 0


def _unpublish(args) -> int:
    from .db import connect_live
    from .publish import unpublish

    n = unpublish(connect(args.db), connect_live(), args.ids, "cli", args.reason)
    print(f"restored {n} site row(s) to the original files")
    return 0


def _sync(args) -> int:
    from dataclasses import asdict

    from .db import connect_live
    from .drive import DriveClient
    from .sync import run

    stats = run(connect(args.db), connect_live(), DriveClient.from_env(),
                log=lambda m: print(m, flush=True))
    print(json.dumps(asdict(stats), indent=2))
    return 0


def _route(args) -> int:
    from dataclasses import asdict

    from .db import connect_live
    from .drive import DriveClient
    from .route import AI
    from .route_job import run

    state_db = connect(args.db)
    ai = AI.from_env(shared=state_db)
    print(f"AI keys: groq {len(ai.groq)}, gemini {len(ai.gemini)}", flush=True)
    stats = run(state_db, connect_live(), DriveClient.from_env(), ai, args.limit,
                args.max_minutes, log=lambda m: print(m, flush=True))
    print(json.dumps(asdict(stats), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pwpipe")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("process", help="clean one PDF")
    p.add_argument("src")
    p.add_argument("dst")
    p.add_argument("--blocklist", default=SEED_PATH)
    p.add_argument("--report")
    p.add_argument("--crops")
    p.set_defaults(func=_process)

    d = sub.add_parser("discover", help="find repeated lines/images across reports")
    d.add_argument("reports", nargs="+")
    d.add_argument("--blocklist", default=SEED_PATH)
    d.add_argument("--min-files", type=int, default=20)
    d.add_argument("--out", default="candidates.csv")
    d.set_defaults(func=_discover)

    a = sub.add_parser("approve", help="fold approved candidates into the blocklist")
    a.add_argument("candidates")
    a.add_argument("--blocklist", default=SEED_PATH)
    a.set_defaults(func=_approve)

    m = sub.add_parser("migrate", help="create/upgrade the state schema")
    m.add_argument("--db")
    m.set_defaults(func=_migrate)

    s = sub.add_parser("seed", help="add a catalog's papers to the state database")
    s.add_argument("catalog_dir")
    s.add_argument("--catalog", required=True, help="source name, e.g. mpboardonline.com")
    s.add_argument("--db")
    s.set_defaults(func=_seed)

    st = sub.add_parser("status", help="papers per catalog / stage / status")
    st.add_argument("--db")
    st.set_defaults(func=_status)

    w = sub.add_parser("work", help="clean papers until the limit or time runs out")
    w.add_argument("--db")
    w.add_argument("--catalog")
    w.add_argument("--limit", type=int, default=50)
    w.add_argument("--max-minutes", type=float, default=300)
    w.add_argument("--owner")
    w.add_argument("--review-all", action="store_true",
                   help="send every paper to review (the pilot)")
    w.add_argument("--blocklist", default=SEED_PATH)
    w.set_defaults(func=_work)

    r = sub.add_parser("review-report", help="local HTML page of staged papers")
    r.add_argument("--db")
    r.add_argument("--catalog")
    r.add_argument("--out", default="work/review")
    r.set_defaults(func=_review_report)

    q = sub.add_parser("requeue", help="send papers back to be cleaned again")
    q.add_argument("--db")
    q.add_argument("--catalog")
    q.add_argument("--stage", nargs="+", default=["staged"])
    q.add_argument("--status", nargs="+", default=["review"])
    q.add_argument("--ids", nargs="*")
    q.add_argument("--reason", required=True)
    q.set_defaults(func=_requeue)

    h = sub.add_parser("health", help="daily health check")
    h.add_argument("--db")
    h.set_defaults(func=_health)

    pb = sub.add_parser("publish", help="point the live site at cleaned copies")
    pb.add_argument("--db")
    pb.add_argument("--limit", type=int, default=2000)
    pb.add_argument("--max-minutes", type=float, default=100)
    pb.add_argument("--owner")
    pb.set_defaults(func=_publish)

    up = sub.add_parser("unpublish", help="restore original links for papers")
    up.add_argument("ids", nargs="+")
    up.add_argument("--reason", required=True)
    up.add_argument("--db")
    up.set_defaults(func=_unpublish)

    sy = sub.add_parser("sync", help="exchange review items and decisions with the site")
    sy.add_argument("--db")
    sy.set_defaults(func=_sync)

    ro = sub.add_parser("route", help="check papers' headers against where they are filed")
    ro.add_argument("--db")
    ro.add_argument("--limit", type=int, default=1500)
    ro.add_argument("--max-minutes", type=float, default=150)
    ro.set_defaults(func=_route)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
