"""Run placement.decide over cached headers and report how it behaves (offline, read-only).

    python scripts/eval_placement.py --site ../paperwala.db --headers ../local/paper_headers_v2.db [--examples 4] [--kind move]
"""
from __future__ import annotations

import argparse
import collections
import random
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pwpipe import placement as P  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--headers", required=True)
    ap.add_argument("--university", default="RGPV")
    ap.add_argument("--examples", type=int, default=3)
    ap.add_argument("--kind", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    site = sqlite3.connect(f"file:{args.site}?mode=ro", uri=True)
    subs = [P.Subject(*r) for r in site.execute(
        "SELECT id, program, branch, semester, code, name, scheme FROM subjects WHERE university = ?", (args.university,))]
    cat = P.Catalog(subs)
    filed = {r[0]: cat.by_id[r[1]] for r in site.execute(
        "SELECT p.id, p.subject_id FROM pyq_papers p JOIN subjects s ON s.id = p.subject_id WHERE s.university = ?", (args.university,))}
    hdr = sqlite3.connect(args.headers)
    rows = hdr.execute("SELECT paper_id, method, text FROM headers").fetchall()

    kinds = collections.Counter()
    by_prog = collections.defaultdict(collections.Counter)
    parsed = collections.Counter()
    methods = collections.Counter()
    samples: dict[tuple, list] = collections.defaultdict(list)
    for pid, method, text in rows:
        methods[method] += 1
        if pid not in filed:
            continue
        s = filed[pid]
        if method in ("text", "ocr"):
            d = P.decide(text, s, cat, ocr=(method == "ocr"))
            f = d.facts
            parsed["readable"] += 1
            for k in ("program", "semester", "code_key", "scheme", "year", "title"):
                if getattr(f, k):
                    parsed[k] += 1
        else:
            d = P.Decision("unverified", None, method)
        kinds[(d.kind, d.reason)] += 1
        by_prog[s.program][d.kind] += 1
        samples[(d.kind, d.reason)].append((pid, s, d, text))

    n = len(rows)
    print(f"{n} headers: {dict(methods)}")
    print("parsed fields among readable:", {k: f"{v} ({100 * v // max(parsed['readable'], 1)}%)" for k, v in parsed.items()})
    print("\nby programme:")
    for prog, c in sorted(by_prog.items(), key=lambda kv: -sum(kv[1].values())):
        t = sum(c.values())
        print(f"  {prog:11} {t:5}  " + "  ".join(f"{k} {v} ({100 * v // t}%)" for k, v in c.most_common()))
    print("\ndecisions:")
    for (k, r), v in sorted(kinds.items(), key=lambda kv: (kv[0][0], -kv[1])):
        print(f"  {k:12} {r:28} {v}")
    random.seed(args.seed)
    for (k, r), items in sorted(samples.items()):
        if (args.kind and k != args.kind) or (args.reason and r != args.reason) or (not args.kind and not args.reason and k == "keep"):
            continue
        print(f"\n######## {k} / {r}  ({len(items)})")
        for pid, s, d, text in random.sample(items, min(args.examples, len(items))):
            f = d.facts
            print(f"- paper {pid} filed: {s.program} sem{s.semester} [{s.code}] {s.name} ({s.scheme or '-'})")
            if f:
                print(f"    header: {f.program} sem{f.semester} code={f.code} scheme={f.scheme or '-'} {f.month} {f.year} title={f.title!r}")
            if d.target_id:
                t = cat.by_id[d.target_id]
                print(f"    -> {t.program} sem{t.semester} [{t.code}] {t.name} ({t.scheme or '-'})")
            print("    " + "; ".join(d.notes)[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
