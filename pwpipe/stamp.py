"""The "paperwala.org" mark, at the top and at the foot of each cleaned page.

It is vector text laid over the page, never pixels painted into the scan:
the scan image stays byte-for-byte what verification compares, and the
unchanged-JPEG passthrough keeps working. Each mark is placed only where
that edge of the page is genuinely blank - no ink and no OCR word under it -
so an edge with no room gets no mark rather than a mark over its content.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

STAMP_TEXT = "paperwala.org"
STAMP_COLOR = (0.35, 0.35, 0.35)
BOX_HEIGHT = 0.028        # of the page height (doubled 2026-10-28, owner's call)
BOX_WIDTH = 0.30          # of the page width; the text needs ~0.21 on A4
SIDE_MARGIN = 0.04        # a corner box keeps this far from the page edge
INK_LEVEL = 160           # a pixel darker than this is ink
MAX_INK = 0.002           # scanner specks tolerated in the zone
# Bottom edges tried, lowest first: margins vary, scanner borders sit lowest.
BOTTOMS = (0.985, 0.975, 0.965, 0.955)
# Top edges tried, highest first, for the mark above the paper's header.
TOPS = (0.015, 0.025, 0.035, 0.045)


def _intersects(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def find_stamp_boxes(img: Image.Image, word_boxes) -> list[tuple[int, int, int, int]]:
    """Blank boxes for the top and bottom marks, in raster pixels: one per
    edge that has room, so zero to two. Centre first; then the corners, since
    a header often runs to the top edge in the middle only ("Quarterly
    Examination" above "Time: 3 hour ... MM: 70")."""
    arr = np.asarray(img.convert("L"))
    h = img.size[1]
    bh = max(24, round(h * BOX_HEIGHT))
    tops = [round(h * t) for t in TOPS]
    bottoms = [round(h * b) - bh for b in BOTTOMS]
    return [box for box in (_blank_box(arr, word_boxes, ys, bh) for ys in (tops, bottoms))
            if box]


def _blank_box(arr, word_boxes, y0s, bh) -> tuple[int, int, int, int] | None:
    h, w = arr.shape
    bw = round(w * BOX_WIDTH)
    margin = round(w * SIDE_MARGIN)
    for x0 in (round(w / 2 - bw / 2), margin, w - margin - bw):
        box = _first_blank(arr, word_boxes, y0s, bh, x0, x0 + bw)
        if box:
            return box
    return None


def _first_blank(arr, word_boxes, y0s, bh, x0, x1) -> tuple[int, int, int, int] | None:
    h, w = arr.shape
    for y0 in y0s:
        y1 = y0 + bh
        zone = arr[max(0, y0 - 4):min(h, y1 + 4), max(0, x0 - 8):min(w, x1 + 8)]
        if zone.size == 0 or (zone < INK_LEVEL).mean() > MAX_INK:
            continue
        box = (x0, y0, x1, y1)
        if any(_intersects(b, box) for b in word_boxes):
            continue
        return box
    return None


def draw_stamp(page, rect) -> None:
    """Write the mark centred in ``rect`` (PDF points) on a PyMuPDF page."""
    import pymupdf

    size = rect.height * 0.8
    width = pymupdf.get_text_length(STAMP_TEXT, fontname="helv", fontsize=size)
    if width > rect.width * 0.95:          # a narrow page: shrink to fit the blank box
        size *= rect.width * 0.95 / width
        width = pymupdf.get_text_length(STAMP_TEXT, fontname="helv", fontsize=size)
    x = rect.x0 + (rect.width - width) / 2
    y = rect.y1 - rect.height * 0.22
    page.insert_text((x, y), STAMP_TEXT, fontsize=size, fontname="helv", color=STAMP_COLOR)
