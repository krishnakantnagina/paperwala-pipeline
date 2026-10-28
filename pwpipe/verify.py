"""Prove a cleaned PDF is safe to publish.

Two independent checks per output page:

1. **Pixels.** Source and output are rasterized at the same size and compared.
   Outside the erased boxes they must match (exactly, for lossless scans);
   inside them the output must be white. This is the proof that no content
   was touched, and it is deterministic.
2. **OCR.** The output page is read again and must contain no ad line. Word
   retention against the source is also measured, but only as a loose
   backstop: erasing a block changes Tesseract's layout analysis, so a few
   words elsewhere legitimately read differently (7 of 121 on a real page).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from .blocklist import Blocklist
from .detect import (REAL_WORD_CONF, PagePlan, _alnum, _tld_word, _url_word,
                     classify_lines, line_labels)
from .ocr import DEFAULT_LANGS, group_lines, ocr_image
from .pdf import PdfInfo, analyze, largest_image, open_pdf, raster_page
from .stamp import STAMP_TEXT
from .rules import AD, SUSPECT, WEAK, _domain_label, line_key

MIN_RETENTION = 0.85
INK_DIFF = 48               # a pixel this much darker/lighter counts as changed
MAX_CHANGED_OUTSIDE = 0.0005  # lossy re-encodes (JPEG, rendering) may flip a few
MIN_WHITE_INSIDE = 0.99
MIN_WORDS_FOR_RATIO = 20     # below this, compare absolute losses instead
MAX_ABS_LOSS = 2
SIZE_RATIO_BOUNDS = (0.05, 5.0)


@dataclass
class VerifyResult:
    ok: bool = True
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    pages: list[dict] = field(default_factory=list)
    # Output rasters by *source* page number, for before/after crops.
    rasters: dict[int, Image.Image] = field(default_factory=dict)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.failures.append(msg)


def _tokens(lines, plan: PagePlan | None = None) -> Counter:
    out: Counter = Counter()
    for line in lines:
        for text, conf, box in line.words:
            if plan is not None and plan.erased(box):
                continue
            key = line_key(text)
            if conf >= REAL_WORD_CONF and sum(c.isalnum() for c in key) >= 2:
                out[key] += 1
    return out


def _pixel_check(before: Image.Image, after: Image.Image, boxes, lossless: bool,
                 ignore=()) -> str | None:
    """``boxes`` must come out white; ``ignore`` (the mark) may hold anything."""
    if before.size != after.size:
        return f"raster size {after.size} != {before.size}"
    a = np.asarray(before.convert("L"), dtype=np.int16)
    b = np.asarray(after.convert("L"), dtype=np.int16)
    inside = np.zeros(a.shape, dtype=bool)
    for x0, y0, x1, y1 in boxes:
        inside[y0:y1, x0:x1] = True
    skip = np.zeros(a.shape, dtype=bool)
    for x0, y0, x1, y1 in ignore:
        skip[y0:y1, x0:x1] = True
    inside &= ~skip
    diff = np.abs(a - b)
    outside = ~inside & ~skip
    if lossless:
        changed = int(np.count_nonzero(diff[outside]))
        if changed:
            return f"{changed} pixels changed outside erased boxes"
    else:
        frac = float(np.count_nonzero(diff[outside] > INK_DIFF)) / max(int(outside.sum()), 1)
        if frac > MAX_CHANGED_OUTSIDE:
            return f"{frac:.4%} of pixels changed outside erased boxes"
    if inside.any():
        white = float(np.count_nonzero(b[inside] >= 200)) / int(inside.sum())
        if white < MIN_WHITE_INSIDE:
            return f"erased boxes only {white:.1%} white"
    return None


def erased_content(plan: PagePlan, labels: list[str]) -> list[str]:
    """Paper words the plan would erase. Must be empty.

    Independent of how regions were built: every confident word inside an
    applied box must itself be ad material - a URL word, or a word of a
    line flagged as ad. This is the check that catches a plan erasing
    "bee loud glade." along with the URL stamped next to it, which the pixel
    check cannot see (it verifies the cleaner against the plan).
    """
    bad = []
    for line in plan.lines:
        for text, conf, box in line.words:
            if conf < REAL_WORD_CONF or _alnum(text) < 2 or not plan.erased(box):
                continue
            if line.kind not in (AD, SUSPECT, WEAK):
                bad.append(text)
            elif line.reason.startswith("domain:") and not (
                    _url_word(text, line_labels(line, labels)) or _tld_word(text)):
                bad.append(text)
    return bad


def verify(src_path: str | Path, src_info: PdfInfo, plans: list[PagePlan], bl: Blocklist,
           dst_path: str | Path, langs: str = DEFAULT_LANGS,
           keep_rasters_for: set[int] | None = None) -> VerifyResult:
    result = VerifyResult()
    kept = [p for p in plans if not p.drop]
    try:
        dst_info = analyze(dst_path)
    except Exception as exc:
        result.fail(f"output unreadable: {exc}")
        return result

    if dst_info.page_count != len(kept):
        result.fail(f"page count {dst_info.page_count} != expected {len(kept)}")
        return result

    ratio = Path(dst_path).stat().st_size / max(Path(src_path).stat().st_size, 1)
    if not SIZE_RATIO_BOUNDS[0] <= ratio <= SIZE_RATIO_BOUNDS[1]:
        result.fail(f"size ratio {ratio:.2f} outside {SIZE_RATIO_BOUNDS}")
    elif ratio > 1.5:
        result.notes.append(f"output {ratio:.2f}x larger than source")

    labels = [_domain_label(d) for d in bl.domains]
    for plan in plans:
        bad = erased_content(plan, labels)
        if bad:
            result.fail(f"page {plan.number}: plan erases paper text: {' '.join(bad)[:80]!r}")

    src_doc = open_pdf(src_path)
    dst_doc = open_pdf(dst_path)
    try:
        for dst_page, plan in zip(dst_info.pages, kept):
            src_page = src_info.pages[plan.number]
            stat = {"source_page": plan.number}
            if abs(dst_page.width - src_page.width) > 0.5 or \
                    abs(dst_page.height - src_page.height) > 0.5:
                result.fail(f"page {plan.number}: size changed")
            # Read both pages the same way. A rebuilt page is compared by its
            # scan image alone (the mark is vector text on top of it); a
            # redacted page is rendered on both sides.
            render = plan.mode == "redact"
            before = raster_page(src_doc, src_page, force_render=render)
            if plan.mode == "rebuild":
                after_img = largest_image(dst_doc, dst_page.number)
            else:
                after_img = raster_page(dst_doc, dst_page, force_render=True).image
            boxes = [b for r in plan.applied_regions for b in r.boxes]
            lossless = plan.mode == "rebuild" and "DCT" not in next(
                i.filter for i in src_page.images if i.xref == src_page.scan_xref)
            ignore = [plan.stamp_box] if plan.stamp_box and render else []
            problem = _pixel_check(before.image, after_img, boxes, lossless, ignore)
            if problem:
                result.fail(f"page {plan.number}: {problem}")
            stat["pixels"] = problem or "ok"

            if plan.stamp_box:
                has_mark = STAMP_TEXT in dst_doc[dst_page.number].get_text()
                stat["stamp"] = "ok" if has_mark else "missing"
                if not has_mark:
                    result.fail(f"page {plan.number}: paperwala.org mark missing")

            lines = classify_lines(group_lines(ocr_image(after_img, langs)), bl)

            ads = [l.text for l in lines if l.kind == AD]
            if ads:
                result.fail(f"page {plan.number}: ad still present: {ads[0][:60]!r}")
            stat["ads_left"] = len(ads)

            expected = _tokens(plan.lines, plan)
            got = _tokens(lines)
            total = sum(expected.values())
            found = sum((expected & got).values())
            stat.update(expected_words=total, retained_words=found)
            if total >= MIN_WORDS_FOR_RATIO:
                retention = found / total
                stat["retention"] = round(retention, 4)
                if retention < MIN_RETENTION:
                    msg = f"page {plan.number}: retention {retention:.3f} < {MIN_RETENTION}"
                    # Lossless and pixel-identical outside the boxes: the words
                    # are provably there; OCR just re-read the layout.
                    (result.notes.append if lossless and not problem else result.fail)(msg)
            elif total - found > MAX_ABS_LOSS:
                msg = f"page {plan.number}: lost {total - found} of {total} words"
                (result.notes.append if lossless and not problem else result.fail)(msg)
            result.pages.append(stat)
            if keep_rasters_for and plan.number in keep_rasters_for:
                result.rasters[plan.number] = after_img
    finally:
        dst_doc.close()
        src_doc.close()
    return result
