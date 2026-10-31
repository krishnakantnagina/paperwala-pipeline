"""Pilot of the cleaner on a stratified sample of one university's papers (offline, resumable).

    python scripts/rgpv_clean_pilot.py --site ../paperwala.db --out ../local/night/clean [--n 120] [--seed 7]

Picks papers spread over programme and year, downloads each public Drive original, runs
pwpipe.process.process_paper on it, and records what happened: pages, whether anything was
erased, review reasons, whether verification passed, seconds. For the production routing
input it also runs placement.decide on the CLEANING REPORT's page-1 text (route.header_text),
not on a fresh read, so the pilot exercises the same path the route job takes.

Writes only under --out. Never touches Drive writes, the state DB or the live site.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import random
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pwpipe import placement as P  # noqa: E402
from pwpipe.blocklist import Blocklist  # noqa: E402
from pwpipe.process import process_paper  # noqa: E402
from pwpipe.route import header_text  # noqa: E402

SEED_PATH = Path(__file__).resolve().parent.parent / "blocklists" / "seed.json"
FIELDS = ["paper_id", "program", "year", "ok", "seconds", "pages", "changed", "pages_dropped", "auto_ok",
          "review", "verify_failures", "placement", "placement_reason", "filed_subject", "error"]


def download(link: str) -> bytes | None:
    m = re.search(r"/d/([\w-]+)", link or "")
    if not m:
        return None
    for attempt in range(3):
        try:
            data = urllib.request.urlopen(urllib.request.Request(
                "https://drive.google.com/uc?export=download&id=" + m.group(1), headers={"User-Agent": "Mozilla/5.0"}), timeout=90).read()
            if data[:4] == b"%PDF":
                return data
        except Exception:
            pass
        time.sleep(1 + attempt)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--university", default="RGPV")
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    out = Path(args.out)
    (out / "pdf").mkdir(parents=True, exist_ok=True)
    (out / "reports").mkdir(exist_ok=True)

    site = sqlite3.connect(f"file:{args.site}?mode=ro", uri=True)
    rows = site.execute("""SELECT p.id, s.program, p.year, p.drive_link, s.id FROM pyq_papers p
                           JOIN subjects s ON s.id = p.subject_id WHERE s.university = ?""", (args.university,)).fetchall()
    subs = [P.Subject(*r) for r in site.execute(
        "SELECT id, program, branch, semester, code, name, scheme FROM subjects WHERE university = ?", (args.university,))]
    cat = P.Catalog(subs)
    rng = random.Random(args.seed)
    strata = collections.defaultdict(list)
    for r in rows:
        strata[(r[1], (r[2] or 0) // 5 * 5)].append(r)
    keys = sorted(strata)
    sample = []
    while len(sample) < args.n and any(strata.values()):
        for k in keys:
            if strata[k] and len(sample) < args.n:
                sample.append(strata[k].pop(rng.randrange(len(strata[k]))))
    done_path = out / "results.csv"
    done = set()
    if done_path.exists():
        done = {int(r["paper_id"]) for r in csv.DictReader(open(done_path, encoding="utf-8-sig"))}
    fh = open(done_path, "a", newline="", encoding="utf-8-sig")
    w = csv.DictWriter(fh, FIELDS)
    if not done:
        w.writeheader()
    bl = Blocklist.load(str(SEED_PATH))
    print(f"{len(sample)} papers in the sample, {len(done)} already done", flush=True)
    for i, (pid, program, year, link, sid) in enumerate(sample, 1):
        if pid in done:
            continue
        row = dict.fromkeys(FIELDS, "")
        row.update(paper_id=pid, program=program, year=year or "", filed_subject=cat.by_id[sid].name)
        t0 = time.time()
        try:
            data = download(link)
            if data is None:
                raise RuntimeError("download failed")
            src = out / "pdf" / f"{pid}_orig.pdf"
            dst = out / "pdf" / f"{pid}_clean.pdf"
            src.write_bytes(data)
            rep = process_paper(src, dst, bl, paper_id=str(pid))
            (out / "reports" / f"{pid}.json").write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
            hdr = header_text(rep)
            d = P.decide(hdr, cat.by_id[sid], cat, ocr=True)
            row.update(ok=int(bool(rep["verify"]["ok"])), pages=len(rep.get("pages", [])), changed=int(bool(rep["changed"])),
                       pages_dropped=len(rep.get("pages_dropped") or []), auto_ok=int(bool(rep.get("auto_ok"))),
                       review="; ".join(map(str, rep.get("review") or []))[:200],
                       verify_failures="; ".join(map(str, rep["verify"].get("failures") or []))[:200],
                       placement=d.kind, placement_reason=d.reason)
            src.unlink(missing_ok=True)       # keep disk small; the cleaned copy and the report stay
        except Exception as exc:
            row.update(ok=0, error=f"{type(exc).__name__}: {exc}"[:200])
        row["seconds"] = round(time.time() - t0, 1)
        w.writerow(row)
        fh.flush()
        print(f"{i}/{len(sample)} paper {pid} {program} ok={row['ok']} {row['seconds']}s placement={row['placement']}", flush=True)
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
