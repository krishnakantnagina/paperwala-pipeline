"""Local command line, for the pilot and for debugging one paper.

    python -m pwpipe process IN.pdf OUT.pdf [--report r.json] [--crops DIR]
    python -m pwpipe discover REPORT.json ... --min-files 20 --out candidates.csv
    python -m pwpipe approve candidates.csv [--blocklist seed.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .blocklist import SEED_PATH, Blocklist
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

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
