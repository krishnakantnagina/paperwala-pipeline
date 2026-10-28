"""Find ad URLs that a printed rule runs through.

A footer like "-----https://www.mpboardonline.com-----" with the rule drawn
through the letters reads as nothing at all to Tesseract (an MP Board
Sanskrit paper the owner sent back, 2026-10-28). Here long horizontal runs of
ink are whitened in a copy of the page, and only a strip around each rule is
read again. Only URL ad lines are taken from that reading; everything else
on the page keeps its first reading.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
from PIL import Image

from .blocklist import Blocklist
from .detect import AD, OcrLine, classify_lines
from .ocr import DEFAULT_LANGS, group_lines, ocr_image

RULE_MIN = 0.12        # a dark horizontal run this long (of page width) is a rule
INK_LEVEL = 128
STRIP = 0.025          # read this far above and below a rule (of page height)
MAX_STRIPS = 6         # a table page has many rules; its URL is not in all of them


def rule_mask(gray: np.ndarray) -> np.ndarray:
    """Pixels that belong to a horizontal run of ink at least RULE_MIN wide."""
    dark = gray < INK_LEVEL
    h, w = dark.shape
    run = max(2, int(w * RULE_MIN))
    if run >= w:
        return np.zeros_like(dark)
    cs = np.zeros((h, w + 1), dtype=np.int32)
    np.cumsum(dark, axis=1, out=cs[:, 1:])
    starts = np.zeros((h, w), dtype=bool)
    starts[:, : w - run + 1] = (cs[:, run:] - cs[:, : w - run + 1]) == run
    # A pixel is on a rule if a full run starts within `run` pixels to its left.
    sc = np.zeros((h, w + 1), dtype=np.int32)
    np.cumsum(starts, axis=1, out=sc[:, 1:])
    lo = np.maximum(np.arange(w) - run + 1, 0)
    return (sc[:, 1:] - sc[:, lo]) > 0


def _strips(rows: np.ndarray, h: int) -> list[tuple[int, int]]:
    reach = max(8, int(h * STRIP))
    out: list[list[int]] = []
    for y in rows:
        y0, y1 = max(0, y - reach), min(h, y + reach)
        if out and y0 <= out[-1][1]:
            out[-1][1] = y1
        else:
            out.append([y0, y1])
    return [tuple(s) for s in out]


def struck_through_ads(img: Image.Image, bl: Blocklist,
                       langs: str = DEFAULT_LANGS) -> list[OcrLine]:
    """URL ad lines readable only once the page's rules are removed, in the
    page's raster pixels."""
    gray = np.asarray(img.convert("L"))
    mask = rule_mask(gray)
    rows = np.flatnonzero(mask.any(axis=1))
    if rows.size == 0:
        return []
    strips = _strips(rows, gray.shape[0])
    if len(strips) > MAX_STRIPS:
        # Keep the outermost ones: footers and headers are where sites stamp.
        strips = strips[: MAX_STRIPS // 2] + strips[-(MAX_STRIPS - MAX_STRIPS // 2):]
    clean = Image.fromarray(np.where(mask, 255, gray).astype(np.uint8))
    found = []
    for y0, y1 in ((int(a), int(b)) for a, b in strips):
        words = [replace(w, y0=w.y0 + y0, y1=w.y1 + y0)
                 for w in ocr_image(clean.crop((0, y0, clean.width, y1)), langs, psm=11)]
        found += [l for l in classify_lines(group_lines(words), bl)
                  if l.kind == AD and l.reason.startswith("domain:")]
    return found
