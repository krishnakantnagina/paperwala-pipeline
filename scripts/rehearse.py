"""Rehearse the router on a COPY of the site database and write a report. Touches nothing else.

    python scripts/rehearse.py --site ../paperwala.db --headers ../local/paper_headers_v2.db --out ../local/night [--hide-unverified]

For every paper with a cached header it runs placement.decide and applies the outcome to the copy:
  move        pyq_papers.subject_id -> target (fenced on the old subject)
  duplicate   same file bytes twice under one subject: the later copy leaves the site
  unsorted    row leaves pyq_papers and goes to unsorted_papers (kept, never shown)
  review / new_subject / unverified   nothing changes; listed in review_queue.csv
Every action is logged to actions.csv (paper id, from, to, reason) so it can be undone.
Then it checks the invariants a real run must keep and lists the subject pages that would empty.
"""
from __future__ import annotations

import argparse
import collections
import csv
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pwpipe import placement as P  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--headers", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--university", default="RGPV")
    ap.add_argument("--hide-unverified", action="store_true", help="also hide papers nothing could be checked for")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    copy = out / "rehearsal.db"
    shutil.copy(args.site, copy)
    db = sqlite3.connect(copy)
    hdr = sqlite3.connect(args.headers)

    subs = [P.Subject(*r) for r in db.execute(
        "SELECT id, program, branch, semester, code, name, scheme FROM subjects WHERE university = ?", (args.university,))]
    cat = P.Catalog(subs)
    papers = db.execute("""SELECT p.id, p.subject_id, p.year, p.exam_type, p.medium, p.title, p.drive_link, p.preview_link, p.file_size, s.program
                           FROM pyq_papers p JOIN subjects s ON s.id = p.subject_id WHERE s.university = ?""", (args.university,)).fetchall()
    h = {r[0]: r[1:] for r in hdr.execute("SELECT paper_id, method, text, sha FROM headers")}
    before = db.execute("SELECT COUNT(*) FROM pyq_papers").fetchone()[0]
    pages_before = {r[0]: r[1] for r in db.execute("SELECT subject_id, COUNT(*) FROM pyq_papers GROUP BY subject_id")}

    # exact duplicates: the same file bytes under one subject
    seen: dict[tuple, int] = {}
    dup: set[int] = set()
    for pid, sid, *_ in sorted(papers):
        sha = (h.get(pid) or ("", "", ""))[2]
        if sha:
            k = (sid, sha)
            if k in seen:
                dup.add(pid)
            else:
                seen[k] = pid

    actions, queue = [], []
    stats = collections.Counter()
    by_prog = collections.defaultdict(collections.Counter)
    stamp = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    for pid, sid, year, exam, medium, title, link, prev, size, program in papers:
        method, text, sha = h.get(pid, ("missing", "", ""))
        filed = cat.by_id[sid]
        if pid in dup:
            d = P.Decision("duplicate", None, "same_bytes")
        elif method in ("text", "ocr"):
            d = P.decide(text, filed, cat, ocr=(method == "ocr"))
        else:
            d = P.Decision("unverified", None, method)
        kind = d.kind
        if kind == "unverified" and args.hide_unverified:
            kind = "unsorted"
        stats[(kind, d.reason)] += 1
        by_prog[program][kind] += 1
        if kind == "move" and d.target_id:
            db.execute("UPDATE pyq_papers SET subject_id = ? WHERE id = ? AND subject_id = ?", (d.target_id, pid, sid))
            actions.append((pid, kind, d.reason, sid, d.target_id, "; ".join(d.notes)[:160]))
        elif kind in ("unsorted", "duplicate"):
            db.execute("""INSERT INTO unsorted_papers (old_paper_id, old_subject_id, old_subject_name, university, program, year, exam_type,
                          medium, title, drive_link, preview_link, file_size, reason, detail, header_text, created_at)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (pid, sid, filed.name, args.university, program, year, exam, medium, title, link, prev, size,
                        "duplicate" if kind == "duplicate" else d.reason, "; ".join(d.notes)[:300], (text or "")[:1500], stamp))
            db.execute("DELETE FROM pyq_papers WHERE id = ? AND subject_id = ?", (pid, sid))
            actions.append((pid, kind, d.reason, sid, "", "; ".join(d.notes)[:160]))
        elif kind in ("review", "new_subject"):
            queue.append((pid, kind, d.reason, sid, d.target_id or "", "; ".join(d.notes)[:200]))
    db.commit()

    after = db.execute("SELECT COUNT(*) FROM pyq_papers").fetchone()[0]
    unsorted = db.execute("SELECT COUNT(*) FROM unsorted_papers").fetchone()[0]
    pages_after = {r[0]: r[1] for r in db.execute("SELECT subject_id, COUNT(*) FROM pyq_papers GROUP BY subject_id")}
    emptied = [sid for sid in pages_before if sid not in pages_after and cat.by_id.get(sid)]

    with open(out / "actions.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["paper_id", "action", "reason", "from_subject", "to_subject", "note"])
        w.writerows(actions)
    with open(out / "review_queue.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["paper_id", "kind", "reason", "filed_subject", "proposed_subject", "note"])
        w.writerows(queue)

    lines = [f"# Rehearsal on {args.university}, {stamp} UTC", "",
             f"papers before **{before}**, on the site after **{after}**, in unsorted_papers **{unsorted}** "
             f"-> invariant {'OK' if after + unsorted == before else 'BROKEN'}",
             f"headers cached: {len(h)} of {len(papers)} papers; hide-unverified: {args.hide_unverified}", "",
             "## Outcomes", "| outcome | reason | papers |", "|---|---|---|"]
    for (k, r), v in sorted(stats.items(), key=lambda kv: (kv[0][0], -kv[1])):
        lines.append(f"| {k} | {r} | {v} |")
    lines += ["", "## By programme", "| programme | " + " | ".join(["keep", "move", "review", "new_subject", "unverified", "unsorted", "duplicate"]) + " |",
              "|---|" + "---|" * 7]
    for prog, c in sorted(by_prog.items(), key=lambda kv: -sum(kv[1].values())):
        lines.append(f"| {prog} | " + " | ".join(str(c.get(k, 0)) for k in ["keep", "move", "review", "new_subject", "unverified", "unsorted", "duplicate"]) + " |")
    lines += ["", f"## Subject pages that would be empty and leave the sitemap: {len(emptied)}"]
    for sid in emptied[:40]:
        s = cat.by_id[sid]
        lines.append(f"- {s.program} sem{s.semester} [{s.code}] {s.name}")
    (out / "pilot_report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:40]))
    return 0 if after + unsorted == before else 1


if __name__ == "__main__":
    sys.exit(main())
