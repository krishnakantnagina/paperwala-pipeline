"""Apply placement decisions from cached headers to a Turso database through the pipeline's own write paths.

    python scripts/apply_placement.py --headers ../local/paper_headers_v2.db --mode propose --limit 50
    python scripts/apply_placement.py --headers ... --mode auto            # moves exact matches too
    python scripts/apply_placement.py --headers ... --mode auto+hide       # and hides what cannot be placed

The target is read from the project .env as STAGING_DATABASE_URL / STAGING_TURSO_AUTH_TOKEN and the script
REFUSES any URL that does not contain "staging": it is the rehearsal on real Turso, not a live-site tool.
Every action is appended to --log (paper id, action, from, to) so it can be undone. --dry-run reads only.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pwpipe import placement as P  # noqa: E402
from pwpipe import route_job  # noqa: E402
from pwpipe.db import METER, TursoDb  # noqa: E402

ENV = Path(__file__).resolve().parent.parent.parent / ".env"


def staging_db() -> TursoDb:
    env = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("\"'")
    url = env["STAGING_DATABASE_URL"].replace("sqlite+libsql://", "libsql://").split("/?")[0]
    if "staging" not in url:
        raise SystemExit(f"refusing: {url.split('.')[0]} is not a staging database")
    return TursoDb(url, env["STAGING_TURSO_AUTH_TOKEN"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headers", required=True)
    ap.add_argument("--mode", choices=["propose", "auto", "auto+hide"], default="propose")
    ap.add_argument("--university", default="RGPV")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="", help="comma list of kinds to act on, e.g. move,review,unsorted; --limit then counts only those")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log", default="../local/night/apply_actions.csv")
    args = ap.parse_args()

    db = staging_db()
    flags = route_job.Flags(placement=True, auto_move=args.mode != "propose", hide_unsorted=args.mode == "auto+hide")
    catalogs = route_job.Catalogs(db)
    cat = catalogs.get(args.university)
    can_hide = flags.hide_unsorted and route_job.unsorted_ready(db)
    rows = db.execute("""SELECT p.id AS id, p.subject_id AS sid, p.title AS title, p.year AS year FROM pyq_papers p
                         JOIN subjects s ON s.id = p.subject_id WHERE s.university = :u ORDER BY p.id""", {"u": args.university})
    hdr = sqlite3.connect(args.headers)
    headers = {r[0]: (r[1], r[2]) for r in hdr.execute("SELECT paper_id, method, text FROM headers WHERE method IN ('text','ocr')")}
    stats = route_job.RouteStats()
    outcome = collections.Counter()
    done = acted = 0
    only = {k for k in args.only.split(",") if k}
    log = Path(args.log)
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "a", newline="", encoding="utf-8-sig")
    w = csv.writer(fh)
    if log.stat().st_size == 0:
        w.writerow(["paper_id", "outcome", "reason", "from_subject", "to_subject", "mode"])
    for r in rows:
        if r["id"] not in headers:
            continue
        if args.limit and (acted if args.only else done) >= args.limit:
            break
        filed = cat.by_id.get(r["sid"])
        if filed is None:
            continue
        method, text = headers[r["id"]]
        place = P.decide(text, filed, cat, ocr=(method == "ocr"))
        outcome[(place.kind, place.reason)] += 1
        done += 1
        if place.kind in ("keep", "unverified") or args.dry_run or (only and place.kind not in only):
            continue
        acted += 1
        pyq = {"id": r["id"], "title": r["title"]}
        subject = {"id": filed.id, "university": args.university, "name": filed.name}
        paper = {"drive_id": f"site{r['id']}"}
        target = place.target_id
        if place.kind == "move" and flags.auto_move and target:
            n = db.execute("UPDATE pyq_papers SET subject_id = :to WHERE id = :p AND subject_id = :fr RETURNING id",
                           {"to": target, "p": r["id"], "fr": filed.id})
            stats.moved += len(n)
            w.writerow([r["id"], "moved", place.reason, filed.id, target, args.mode])
        elif place.kind == "unsorted" and can_hide:
            stats.hidden += route_job.hide_paper(db, pyq, subject, place, text)
            w.writerow([r["id"], "hidden", place.reason, filed.id, "", args.mode])
        else:
            reason = {"move": "move_subject", "review": "placement_review", "new_subject": "new_subject",
                      "unsorted": "unsorted"}.get(place.kind, "placement_review")
            route_job.propose_review(db, paper, pyq, subject, reason, place, cat.by_id[target].name if target else "")
            stats.reviews += 1
            w.writerow([r["id"], "proposed", place.reason, filed.id, target or "", args.mode])
        fh.flush()
    print(f"{done} papers checked ({'dry run' if args.dry_run else args.mode}) -> moved {stats.moved}, hidden {stats.hidden}, proposals {stats.reviews}")
    for (k, rsn), v in sorted(outcome.items(), key=lambda kv: (kv[0][0], -kv[1])):
        print(f"  {k:12} {rsn:28} {v}")
    print(METER.summary() if hasattr(METER, "summary") else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
