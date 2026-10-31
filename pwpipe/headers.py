"""Read the identity block at the top of a question paper's first page.

Everything the router needs to file a paper correctly is printed there: the
programme ("B.E. III Semester"), the paper code and scheme ("CE-221-CBCS"), the
subject, the session ("June 2020"). This module only turns a PDF into that text.
`placement.py` turns the text into a decision.

* Papers with a text layer: the text blocks in the top part of page 1.
* Scans: that top part is rendered and run through tesseract. Only the header is
  OCR'd (about a third of the page), which is several times cheaper than OCR of
  the whole page and is all the router reads.

For papers the cleaning pipeline has already processed, the same text exists in
the cleaning report (`route.header_text`); this module is for papers that have
not been through cleaning yet and for offline evaluation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import pymupdf

from . import ocr, pdf

HEADER_SHARE = 0.36          # the identity block sits in the top third of page 1
MIN_TEXT = 40                # fewer letters than this and the text layer is not a header
OCR_DPI = 220


@dataclass
class HeaderRead:
    text: str
    method: str              # "text" | "ocr" | "empty"
    pages: int


def _clean(text: str) -> str:
    text = text.replace("­", "").replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _letters(text: str) -> int:
    return len(re.findall(r"[A-Za-zऀ-ॿ0-9]", text))


def _text_layer(page: pymupdf.Page) -> str:
    """Text blocks that start in the header zone, top to bottom."""
    limit = page.rect.y0 + page.rect.height * HEADER_SHARE
    blocks = [b for b in page.get_text("blocks") if b[6] == 0 and b[1] < limit]
    blocks.sort(key=lambda b: (round(b[1] / 6), b[0]))
    return _clean("\n".join(b[4] for b in blocks))


def _ocr_header(page: pymupdf.Page, langs: str) -> str:
    r = page.rect
    clip = pymupdf.Rect(r.x0, r.y0, r.x1, r.y0 + r.height * HEADER_SHARE)
    pix = page.get_pixmap(dpi=OCR_DPI, clip=clip, colorspace=pymupdf.csGRAY, alpha=False)
    words = ocr.ocr_image(pdf.pixmap_to_image(pix), langs=langs, psm=6, timeout=120)
    return _clean("\n".join(line.text for line in ocr.group_lines(words)))


def read_header(data: bytes, langs: str = "eng") -> HeaderRead:
    """The header text of page 1 of the PDF in `data`. Never raises for a bad PDF:
    the method says "empty" and the router leaves the paper alone."""
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        return HeaderRead("", "empty", 0)
    try:
        if doc.page_count == 0:
            return HeaderRead("", "empty", 0)
        page = doc[0]
        text = _text_layer(page)
        if _letters(text) >= MIN_TEXT:
            return HeaderRead(text, "text", doc.page_count)
        try:
            text = _ocr_header(page, langs)
        except Exception:
            return HeaderRead("", "empty", doc.page_count)
        return HeaderRead(text, "ocr" if _letters(text) >= 8 else "empty", doc.page_count)
    finally:
        doc.close()
