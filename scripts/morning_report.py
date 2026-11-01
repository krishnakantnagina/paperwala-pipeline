"""Collect everything the overnight run produced into one readable file: local/night/MORNING.md. Reads files only."""
from __future__ import annotations

import collections
import csv
import json
import sqlite3
import statistics
import sys
from pathlib import Path

N = Path(sys.argv[1] if len(sys.argv) > 1 else "../local/night")


def read(p: Path, limit: int = 0) -> str:
    if not p.exists():
        return f"(missing: {p.name})"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[:limit] if limit else lines)


def clean_stats(d: Path) -> str:
    f = d / "results.csv"
    if not f.exists():
        return "(not run)"
    R = list(csv.DictReader(open(f, encoding="utf-8-sig")))
    if not R:
        return "(empty)"
    rev = collections.Counter(x[:50] for r in R for x in (r["review"] or "").split("; ") if x)
    vf = collections.Counter(x[:60] for r in R for x in (r["verify_failures"] or "").split("; ") if x)
    pl = collections.Counter((r["placement"]) for r in R)
    return (f"{len(R)} papers; verify ok {sum(r['ok'] == '1' for r in R)}; errors {sum(bool(r['error']) for r in R)}; "
            f"something erased on {sum(r['changed'] == '1' for r in R)}; median {statistics.median(float(r['seconds']) for r in R):.0f}s\n"
            f"  review reasons: {dict(rev)}\n  verify failures: {dict(vf)}\n  placement on the cleaning report header: {dict(pl)}")


out = ["# Morning report", "", "Written by scripts/morning_report.py from files only. Read ALERT.txt first if it exists.", ""]
alert = N / "ALERT.txt"
if alert.exists():
    out += ["## ALERT", read(alert), ""]
out += ["## Turso usage (pipeline-data org, every 10 minutes)", "```", read(N / "turso_usage.log").splitlines()[-1] if (N / "turso_usage.log").exists() else "(no log)", "```",
        "first and peak:", "```", *(read(N / "turso_usage.log").splitlines()[:1] + read(N / "turso_usage.log").splitlines()[-1:]), "```", ""]

h = N.parent / "paper_headers_v2.db"
if h.exists():
    c = sqlite3.connect(h)
    out += ["## Headers read", str(dict(c.execute("SELECT method, COUNT(*) FROM headers GROUP BY method").fetchall())), ""]

for run in ("run_a", "run_b"):
    out += [f"## Rehearsal {run} ({'unverified papers stay on the site' if run == 'run_a' else 'unverified papers hidden'})",
            read(N / run / "pilot_report.md", 70), ""]
out += ["## Staging apply (propose mode, real Turso)", read(N / "after.log").split("== apply ==")[-1][:2500] if (N / "after.log").exists() else "", ""]
out += ["## Cleaning pilot 1 (120 papers)", clean_stats(N / "clean"), "", "## Cleaning pilot 2 (larger, with the new domain rule)", clean_stats(N / "clean2"), ""]
old = N / "old_review" / "summary.json"
out += ["## Old org review data (to learn from)", read(old) if old.exists() else "(not extracted yet: the old org was still blocked, or the watcher is still waiting)", ""]
out += ["## Evaluation examples (first lines)", "```", read(N / "eval_examples.txt", 60), "```", ""]
(N / "MORNING.md").write_text("\n".join(out), encoding="utf-8")
print("wrote", N / "MORNING.md")
