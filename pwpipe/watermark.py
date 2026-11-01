"""Remove a grey diagonal (or sideways) watermark that is printed lighter than the paper's own ink.

Some sources stamp their name across the page ("RGPVonline.com" at 45 degrees, mid-grey) and the cleaner's
box erase can not touch it without cutting the questions under it. But its pixels sit in a grey band that
the paper's text (near black) does not: ink is below ~100, the stamp is about 130-205. So pixels in that
band, away from solid ink, are the stamp, and whitening them (plus a thin fringe, never anything darker
than ``KEEP_BELOW``) removes it and leaves the text.

Only applied when the grey pixels form a long stretched shape (a line of letters), never for scanner
grey or a halftone picture.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter

GREY_LO, GREY_HI = 120, 210
KEEP_BELOW = 105             # darker than this is paper ink, never touched
MIN_PIXELS = 1500            # a few letters of watermark at 600 px wide; less is noise
MIN_ELONGATION = 4.0         # length / thickness of the whole shape
MAX_PAGE_FRACTION = 0.08     # more grey than this is a photo or a tinted page, not a stamp


def grey_stamp_mask(gray: np.ndarray) -> np.ndarray | None:
    """Pixels of a grey stamp, or None when the page has none."""
    near_ink = Image.fromarray(((gray < 100) * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(7))
    grey = (gray >= GREY_LO) & (gray <= GREY_HI) & (np.asarray(near_ink) == 0)
    solid = Image.fromarray((grey * 255).astype(np.uint8)).filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
    core = np.asarray(solid) > 0
    n = int(core.sum())
    if n < MIN_PIXELS or n > MAX_PAGE_FRACTION * gray.size:
        return None
    ys, xs = np.nonzero(core)
    pts = np.stack([xs, ys], 1).astype(float)
    sv = np.linalg.svd(pts - pts.mean(0), compute_uv=False)
    if sv[1] <= 0 or sv[0] / sv[1] < MIN_ELONGATION:
        return None
    # the fringe round each letter: anti-aliased edge pixels, but only lighter than ink
    grown = np.asarray(Image.fromarray((core * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(9))) > 0
    return grown & (gray >= KEEP_BELOW)


def erase_grey_stamp(img: Image.Image) -> tuple[Image.Image, int]:
    """The page without its grey stamp, and how many pixels were whitened (0: untouched)."""
    gray = np.asarray(img.convert("L"))
    mask = grey_stamp_mask(gray)
    if mask is None:
        return img, 0
    out = np.asarray(img).copy()
    out[mask] = 255
    return Image.fromarray(out, img.mode), int(mask.sum())
