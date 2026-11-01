"""Find ad URLs printed sideways (a vertical "http://www.upboardonline.com" down the middle of every page).

The ordinary reading only sees horizontal text, so a vertical link is read as noise, never erased, and the
verifier - which reads the same way - passes the page. The owner sent such a paper back (UP Board Civics 2019,
"vertical link is still there in centre of every page"). Here the page is turned a quarter each way and read
again, in English only (URLs are Latin). Only URL ad lines are kept; their word boxes are mapped back to the
page's own pixels, where they are narrow tall boxes that the planner erases like any other.
"""

from __future__ import annotations

from dataclasses import replace

from PIL import Image

from .blocklist import Blocklist
from .detect import AD, OcrLine, classify_lines
from .ocr import group_lines, ocr_image

# (PIL transpose, name): turned counter-clockwise a quarter, and clockwise a quarter.
_TURNS = ((Image.Transpose.ROTATE_90, "ccw"), (Image.Transpose.ROTATE_270, "cw"))


def _back(w, turn: str, width: int, height: int):
    """Map a word box read on the turned page onto the original page."""
    if turn == "ccw":      # turned: x' = y, y' = width-1-x   ->  x = width-1-y', y = x'
        x0, x1 = width - w.y1, width - w.y0
        y0, y1 = w.x0, w.x1
    else:                  # turned: x' = height-1-y, y' = x   ->  x = y', y = height-1-x'
        x0, x1 = w.y0, w.y1
        y0, y1 = height - w.x1, height - w.x0
    return replace(w, x0=x0, y0=y0, x1=x1, y1=y1)


def rotated_ads(img: Image.Image, bl: Blocklist) -> list[OcrLine]:
    """URL ad lines printed sideways, in the page's raster pixels."""
    width, height = img.size
    found: list[OcrLine] = []
    for transpose, turn in _TURNS:
        words = ocr_image(img.transpose(transpose), "eng", psm=11)
        words = [_back(w, turn, width, height) for w in words]
        for line in classify_lines(group_lines(words), bl):
            if line.kind == AD and line.reason.startswith("domain:"):
                found.append(line)
    return found
