"""Cache the first-page header of every paper of one university (resumable).

    python scripts/rgpv_headers.py --site ../paperwala.db --out ../local/paper_headers_v2.db [--university RGPV] [--limit N] [--workers 6]

Reads the paper list from a LOCAL copy of the site database (never the live one),
downloads each public Drive file, extracts the header with pwpipe.headers and stores
it. Safe to stop and restart: papers already in --out are skipped. Writes nothing
anywhere except --out.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pwpipe import headers  # noqa: E402


def drive_id(link: str) -> str | None:
    m = re.search(r"/d/([\w-]+)", link or "")
    return m.group(1) if m else None


def download(fid: str) -> bytes | None:
    url = "https://drive.google.com/uc?export=download&id=" + fid
    for attempt in range(3):
        try:
            data = urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=60).read()
            if data[:4] == b"%PDF":
                return data
        except Exception:
            pass
        time.sleep(1 + attempt)
    return None


def work(paper_id: int, link: str, langs: str):
    fid = drive_id(link)
    if not fid:
        return paper_id, None, "badlink", "", 0, "", 0, 0
    data = download(fid)
    if data is None:
        return paper_id, fid, "download_failed", "", 0, "", 0, 0
    t0 = time.time()
    got = headers.read_header(data, langs)
    return (paper_id, fid, got.method, got.text[:4000], got.pages,
            hashlib.sha256(data).hexdigest()[:16], len(data), int((time.time() - t0) * 1000))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--university", default="RGPV")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--langs", default="eng")
    ap.add_argument("--retry-failed", action="store_true")
    args = ap.parse_args()

    out = sqlite3.connect(args.out)
    out.execute("""CREATE TABLE IF NOT EXISTS headers (paper_id INTEGER PRIMARY KEY, drive_id TEXT, method TEXT,
                   text TEXT, pages INTEGER, sha TEXT, bytes INTEGER, ms INTEGER)""")
    site = sqlite3.connect(f"file:{args.site}?mode=ro", uri=True)
    rows = site.execute("""SELECT p.id, p.drive_link FROM pyq_papers p JOIN subjects s ON s.id = p.subject_id
                           WHERE s.university = ? ORDER BY p.id""", (args.university,)).fetchall()
    done = {r[0] for r in out.execute(
        "SELECT paper_id FROM headers" + ("" if not args.retry_failed else " WHERE method NOT IN ('badlink','download_failed')"))}
    todo = [(p, l) for p, l in rows if p not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{args.university}: {len(rows)} papers, {len(done)} cached, {len(todo)} to do", flush=True)
    n = 0
    t0 = time.time()
    with cf.ThreadPoolExecutor(args.workers) as ex:
        futs = [ex.submit(work, p, l, args.langs) for p, l in todo]
        for f in cf.as_completed(futs):
            out.execute("INSERT OR REPLACE INTO headers VALUES (?,?,?,?,?,?,?,?)", f.result())
            n += 1
            if n % 100 == 0:
                out.commit()
                rate = n / (time.time() - t0)
                print(f"{n}/{len(todo)}  {rate:.1f}/s  eta {int((len(todo) - n) / max(rate, 0.01) / 60)} min", flush=True)
    out.commit()
    stats = dict(out.execute("SELECT method, COUNT(*) FROM headers GROUP BY method").fetchall())
    print("DONE", stats, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
