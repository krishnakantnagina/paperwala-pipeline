"""The "paperwala.org" mark at the foot of each cleaned page.

It is vector text laid over the page, never pixels painted into the scan:
the scan image stays byte-for-byte what verification compares, and the
unchanged-JPEG passthrough keeps working. It is placed only where the
bottom of the page is genuinely blank - no ink and no OCR word under it -
so a page with no room gets no mark rather than a mark over its content.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

STAMP_TEXT = "paperwala.org"
STAMP_COLOR = (0.35, 0.35, 0.35)
BOX_HEIGHT = 0.014        # of the page height
BOX_WIDTH = 0.24          # of the page width
INK_LEVEL = 160           # a pixel darker than this is ink
MAX_INK = 0.002           # scanner specks tolerated in the zone
# Bottom edges tried, lowest first: margins vary, scanner borders sit lowest.
BOTTOMS = (0.985, 0.975, 0.965, 0.955)


def _intersects(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def find_stamp_box(img: Image.Image, word_boxes) -> tuple[int, int, int, int] | None:
    """A blank box at the bottom centre of the raster, in raster pixels."""
    w, h = img.size
    bh, bw = max(12, round(h * BOX_HEIGHT)), round(w * BOX_WIDTH)
    arr = np.asarray(img.convert("L"))
    x0 = round(w / 2 - bw / 2)
    x1 = x0 + bw
    for bottom in BOTTOMS:
        y1 = round(h * bottom)
        y0 = y1 - bh
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
    x = rect.x0 + (rect.width - width) / 2
    y = rect.y1 - rect.height * 0.22
    page.insert_text((x, y), STAMP_TEXT, fontsize=size, fontname="helv", color=STAMP_COLOR)
