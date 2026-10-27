"""A local HTML page for reviewing staged papers (until the admin pages exist).

    python -m pwpipe review-report --catalog mpboardonline.com --out work/review

Downloads each paper's report and crops from Drive into ``--out`` and writes
``index.html`` there: per paper, why it is in review, what was erased (with
before/after crops), anything suspect, and the verification result.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from . import state
from .db import Db
from .drive import DriveClient


def _esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def build(db: Db, drive: DriveClient, out: Path, catalog: str | None, limit: int = 500) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    rows = db.execute(
        """SELECT * FROM papers WHERE stage IN ('staged', 'approved')
           AND (:c IS NULL OR catalog = :c) ORDER BY status DESC, title LIMIT :n""",
        {"c": catalog, "n": limit})
    failed = db.execute(
        """SELECT drive_id, title, path, status, attempts, last_error FROM papers
           WHERE last_error IS NOT NULL AND (:c IS NULL OR catalog = :c)
           ORDER BY status, title""", {"c": catalog})

    cards = []
    for row in rows:
        pid = row["drive_id"]
        pdir = out / pid
        pdir.mkdir(exist_ok=True)
        rpath = pdir / "report.json"
        if not rpath.exists() and row.get("report_ref"):
            drive.download(row["report_ref"], rpath)
        report = json.loads(rpath.read_text(encoding="utf-8")) if rpath.exists() else {}
        crops = []
        if row.get("crops_ref"):
            for f in drive.list_folder(row["crops_ref"]):
                dest = pdir / f["name"]
                if not dest.exists():
                    drive.download(f["id"], dest)
                crops.append(f["name"])
        cards.append(_card(row, report, sorted(crops)))

    reasons: dict[str, int] = {}
    for row in rows:
        for r in json.loads(row.get("review_reasons") or "[]"):
            reasons[r] = reasons.get(r, 0) + 1
    changed = sum(1 for c in cards if 'class="erased"' in c)

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pipeline review</title>
<style>
:root {{ --bg:#f4f5f7; --card:#fff; --ink:#1d2330; --muted:#5b6475; --line:#d5d9e0;
        --red:#b42318; --amber:#9a5b00; --green:#18794e; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#14161b; --card:#1d2027; --ink:#e7e9ee;
        --muted:#a3abba; --line:#353a45; --red:#ff8a80; --amber:#ffc46b; --green:#6fdba5; }} }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui,sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:24px 16px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:16px; margin:14px 0; }}
h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:0 0 6px; }}
.muted {{ color:var(--muted); font-size:13px; }}
.tag {{ display:inline-block; border:1px solid var(--line); border-radius:99px;
        padding:0 8px; margin:2px 4px 2px 0; font-size:12px; }}
.ok {{ color:var(--green); }} .bad {{ color:var(--red); }} .warn {{ color:var(--amber); }}
img {{ max-width:100%; border:1px solid var(--line); border-radius:6px; margin:6px 0; }}
ul {{ margin:6px 0; padding-left:20px; }} code {{ font-size:13px; }}
</style></head><body><main>
<h1>Pipeline review{(' - ' + _esc(catalog)) if catalog else ''}</h1>
<p class="muted">{len(rows)} papers staged, {changed} with something erased,
{len(failed)} with errors. Reasons: {_esc(', '.join(f'{k} {v}' for k, v in sorted(reasons.items())))}</p>
{_failed_block(failed)}
{''.join(cards)}
</main></body></html>"""
    index = out / "index.html"
    index.write_text(page, encoding="utf-8")
    return index


def _failed_block(failed: list[dict]) -> str:
    if not failed:
        return ""
    items = "".join(
        f"<li><b>{_esc(f['title'])}</b> <span class='muted'>{_esc(f['path'])}</span> - "
        f"{_esc(f['status'])}, attempt {_esc(f['attempts'])}<br>"
        f"<code>{_esc((f['last_error'] or '').splitlines()[0][:200])}</code></li>"
        for f in failed)
    return f"<div class='card'><h2 class='bad'>Errors</h2><ul>{items}</ul></div>"


def _card(row: dict, report: dict, crops: list[str]) -> str:
    pid = row["drive_id"]
    reasons = json.loads(row.get("review_reasons") or "[]")
    verify = report.get("verify", {})
    vline = ("<span class='ok'>verified</span>" if verify.get("ok")
             else f"<span class='bad'>verify failed: {_esc('; '.join(verify.get('failures', [])))}</span>")
    regions, suspects = [], []
    for page in report.get("pages", []):
        for r in page.get("regions", []):
            state_ = "erased" if not r.get("conflicts") else "blocked"
            regions.append(
                f"<li class='{state_}'>page {page['number'] + 1}: <b>{state_}</b> - "
                f"{_esc(' / '.join(r['lines'])[:300])}"
                + (f"<br><span class='warn'>touches: {_esc('; '.join(r['conflicts'])[:200])}</span>"
                   if r.get("conflicts") else "") + "</li>")
        for s in page.get("suspects", []):
            suspects.append(f"<li>page {page['number'] + 1}: {_esc(s['text'][:200])} "
                            f"<span class='muted'>({_esc(s['reason'])})</span></li>")
        if page.get("drop"):
            regions.append(f"<li class='erased'>page {page['number'] + 1}: <b>dropped</b> (only ads)</li>")
    imgs = "".join(f"<img src='{_esc(pid)}/{_esc(c)}' alt='{_esc(c)}' loading='lazy'>" for c in crops)
    drive_link = (f"<a href='https://drive.google.com/file/d/{_esc(row['output_file_id'])}/view' "
                  f"target='_blank' rel='noopener'>cleaned PDF</a>" if row.get("output_file_id") else "")
    orig_link = (f"<a href='https://drive.google.com/file/d/{_esc(pid)}/view' target='_blank' "
                 f"rel='noopener'>original</a>")
    return f"""<div class="card">
<h2>{_esc(row['title'])}</h2>
<div class="muted">{_esc(row['path'])} &middot; {report.get('page_count', '?')} pages &middot;
{vline} &middot; {orig_link} &middot; {drive_link}</div>
<div>{''.join(f"<span class='tag'>{_esc(r)}</span>" for r in reasons)}</div>
{'<ul>' + ''.join(regions) + '</ul>' if regions else '<p class="muted">Nothing erased.</p>'}
{'<p><b>Suspect, left in place:</b></p><ul>' + ''.join(suspects) + '</ul>' if suspects else ''}
{imgs}
</div>"""
