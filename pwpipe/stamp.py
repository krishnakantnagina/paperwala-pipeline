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


def find_stamp_boxes(img: Image.Image, word_boxes, visible=None) -> list[tuple[int, int, int, int]]:
    """Blank boxes for the top and bottom marks, in raster pixels: one per
    edge that has room, so zero to two. Centre first; then the corners, since
    a header often runs to the top edge in the middle only ("Quarterly
    Examination" above "Time: 3 hour ... MM: 70").

    ``visible`` is the part of the raster that lies on the PDF page, as a pixel
    box. A scan can be larger than its page (cropped by the page box): the
    page's edges are then inside the picture, and a mark placed at the
    picture's edge would be drawn off the page and never seen (2026-11-01, 3 of
    500 pilot papers)."""
    full = img.convert("L")
    vx0, vy0, vx1, vy1 = 0, 0, full.size[0], full.size[1]
    if visible:
        vx0, vy0 = max(0, int(np.ceil(visible[0]))), max(0, int(np.ceil(visible[1])))
        vx1, vy1 = min(full.size[0], int(visible[2])), min(full.size[1], int(visible[3]))
        if vx1 - vx0 < 50 or vy1 - vy0 < 50:
            return []
    arr = np.asarray(full)[vy0:vy1, vx0:vx1]
    words = [(b[0] - vx0, b[1] - vy0, b[2] - vx0, b[3] - vy0) for b in word_boxes]
    h = arr.shape[0]
    bh = max(24, round(h * BOX_HEIGHT))
    tops = [round(h * t) for t in TOPS]
    bottoms = [round(h * b) - bh for b in BOTTOMS]
    found = [box for box in (_blank_box(arr, words, ys, bh) for ys in (tops, bottoms)) if box]
    return [(x0 + vx0, y0 + vy0, x1 + vx0, y1 + vy0) for x0, y0, x1, y1 in found]


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
    pt, rotate = _upright(page, pymupdf.Point(x, y))
    page.insert_text(pt, STAMP_TEXT, fontsize=size, fontname="helv", color=STAMP_COLOR, rotate=rotate)


def _upright(page, pt):
    """``pt`` given as the reader sees the page, as (point, rotate) for ``insert_text``.

    ``page.rect`` and the rendered raster the boxes come from are the page as displayed, but
    ``insert_text`` writes in unrotated PDF space. On a /Rotate 90/270 page (AKTU scans saved
    sideways, 3 of 54 in the 2026-11-04 sample) the marks landed sideways or off the page, the
    pixel check failed and the paper went to review. Derotating the point and turning the text
    by the page's own rotation puts it where it was planned, reading upright."""
    if not page.rotation:
        return pt, 0
    return pt * page.derotation_matrix, page.rotation


# Tiled diagonal mark: 3 parallel lines at 25 degrees that cut the page into 4 bands,
# two copies of the text on each line, faint enough to read the paper through it.
DIAG_TEXT = "paperwala.org"
DIAG_SIZE = 24            # PDF points
DIAG_ANGLE = 25           # degrees, rising to the right
DIAG_OPACITY = 0.33
DIAG_GREY = 0.45


def _chord(cx, cy, ux, uy, w, h):
    """Parameter range [t0, t1] where the point (cx+t*ux, cy+t*uy) is inside the page."""
    t0, t1 = -1e9, 1e9
    for c, u, lim in ((cx, ux, w), (cy, uy, h)):
        if abs(u) < 1e-9:
            if not 0 <= c <= lim:
                return None
            continue
        a, b = (0 - c) / u, (lim - c) / u
        t0, t1 = max(t0, min(a, b)), min(t1, max(a, b))
    return (t0, t1) if t1 > t0 else None


def draw_diagonal_marks(page, text: str = DIAG_TEXT, size: float = DIAG_SIZE,
                        angle: float = DIAG_ANGLE, opacity: float = DIAG_OPACITY) -> None:
    """Draw 3 parallel lines x 2 copies of ``text`` on a PyMuPDF page.

    The lines are ``angle`` degrees above horizontal and sit at 25/50/75 % of the page
    measured across them, so they cut it into four bands. The two copies on a line
    are centred at 30% and 70% of the part of the line that lies on the page
    (the size shrinks on a page too small for 24 pt).
    """
    import math
    import pymupdf

    w, h = page.rect.width, page.rect.height
    th = math.radians(angle)
    ux, uy = math.cos(th), -math.sin(th)          # along the line (y grows downward)
    nx, ny = math.sin(th), math.cos(th)           # across the lines
    cx, cy = w / 2, h / 2
    half = (abs(nx) * w + abs(ny) * h) / 2        # half the page's extent across the lines
    lines = []
    for k in (-0.5, 0.0, 0.5):
        lx, ly = cx + k * half * nx, cy + k * half * ny
        chord = _chord(lx, ly, ux, uy, w, h)
        if chord is not None:
            lines.append((lx, ly, chord))
    if not lines:
        return
    # Copies sit at 30 % and 70 % of the part of the line that is on the page. On a small page
    # the shortest line cannot hold 24 pt of text, so the size shrinks until both copies fit.
    width = pymupdf.get_text_length(text, fontname="helv", fontsize=size)
    shortest = min(t1 - t0 for _, _, (t0, t1) in lines)
    if width > 0.36 * shortest:
        size = max(6.0, size * 0.36 * shortest / width)
        width = pymupdf.get_text_length(text, fontname="helv", fontsize=size)
    for lx, ly, (t0, t1) in lines:
        for frac in (0.3, 0.7):
            t = t0 + (t1 - t0) * frac
            # text starts half its width back along the line, so it is centred on t
            pt, rotate = _upright(page, pymupdf.Point(lx + (t - width / 2) * ux, ly + (t - width / 2) * uy))
            page.insert_text(pt, text, fontsize=size, fontname="helv",
                             color=(DIAG_GREY,) * 3, fill_opacity=opacity,
                             morph=(pt, pymupdf.Matrix(angle)), rotate=rotate)


def add_diagonal_marks(path) -> int:
    """Draw the diagonal marks on every page of the finished PDF at ``path``.

    A separate pass after cleaning and verification, so neither has to know about the
    marks. Written to a side file and renamed. Returns how many marks the file now holds.
    """
    from pathlib import Path
    import pymupdf

    path = Path(path)
    doc = pymupdf.open(path)
    try:
        for page in doc:
            draw_diagonal_marks(page)
        tmp = Path(str(path) + ".mark")
        doc.save(tmp, garbage=4, deflate=True)
        pages = doc.page_count
    finally:
        doc.close()
    check = pymupdf.open(tmp)
    try:
        found = sum(
            "".join(sp["text"] for sp in ln["spans"]).count(DIAG_TEXT)
            for p in check for b in p.get_text("dict")["blocks"] for ln in b.get("lines", ())
            # tilted text only (extraction may join two marks). Directions come back in unrotated
            # space, so on a /Rotate 90 page the upright corner marks read as (0, 1): count only
            # text that is off both axes.
            if abs(ln["dir"][0]) > 0.1 and abs(ln["dir"][1]) > 0.1)
    finally:
        check.close()
    if found < pages * 6:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"expected {pages * 6} diagonal marks, found {found}")
    tmp.replace(path)
    return found
