"""Read a PDF's structure and get one raster per page to OCR.

A page is a *scan page* when it is nothing but one upright image covering the
page. Those are rebuilt from the bare scan (clean.py), which drops every
overlay the source sites stacked on top: the zero-opacity A-PDF logo, the
white box over the old URL footer, anything else. Every other page is edited
in place with redactions. The scan test is strict on purpose - anything
unusual (rotation, offset boxes, masks, text) takes the slower redaction path
rather than a rebuild that might misplace the image.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
from PIL import Image

RENDER_DPI = 200
SCAN_MIN_COVERAGE = 0.85


@dataclass
class ImageUse:
    xref: int
    sha1: str
    width: int
    height: int
    bpc: int
    filter: str
    rects: list[tuple[float, float, float, float]]


@dataclass
class PageInfo:
    number: int
    width: float
    height: float
    rotation: int
    text_chars: int
    images: list[ImageUse] = field(default_factory=list)
    scan_xref: int | None = None
    scan_rect: tuple[float, float, float, float] | None = None

    @property
    def is_scan(self) -> bool:
        return self.scan_xref is not None


@dataclass
class PdfInfo:
    path: str
    sha256: str
    page_count: int
    metadata: dict
    pages: list[PageInfo]


class PdfError(RuntimeError):
    pass


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def open_pdf(path: str | Path) -> pymupdf.Document:
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # mupdf raises several types for a broken file
        raise PdfError(f"cannot open: {exc}") from exc
    if not doc.is_pdf:
        raise PdfError("not a PDF")
    if doc.needs_pass:
        raise PdfError("password protected")
    if doc.page_count == 0:
        raise PdfError("no pages")
    return doc


def image_sha1(doc: pymupdf.Document, xref: int) -> str:
    raw = doc.xref_stream_raw(xref) or b""
    return hashlib.sha1(raw).hexdigest()


def _key(doc: pymupdf.Document, xref: int, key: str) -> str:
    kind, value = doc.xref_get_key(xref, key)
    return "" if kind == "null" else value


def analyze(path: str | Path) -> PdfInfo:
    doc = open_pdf(path)
    try:
        pages = [_analyze_page(doc, page) for page in doc]
        return PdfInfo(path=str(path), sha256=file_sha256(path), page_count=doc.page_count,
                       metadata=dict(doc.metadata or {}), pages=pages)
    finally:
        doc.close()


def _analyze_page(doc: pymupdf.Document, page: pymupdf.Page) -> PageInfo:
    info = PageInfo(number=page.number, width=page.rect.width, height=page.rect.height,
                    rotation=page.rotation,
                    text_chars=len("".join(page.get_text("text").split())))
    seen: set[int] = set()
    for item in page.get_images(full=True):
        xref = item[0]
        if xref in seen:
            continue
        seen.add(xref)
        placements = page.get_image_rects(xref, transform=True)
        bpc = _key(doc, xref, "BitsPerComponent")
        info.images.append(ImageUse(
            xref=xref, sha1=image_sha1(doc, xref), width=item[2], height=item[3],
            bpc=int(bpc) if bpc.isdigit() else 0, filter=_key(doc, xref, "Filter"),
            rects=[tuple(r) for r, _ in placements],
        ))
    _detect_scan(doc, page, info)
    return info


def _detect_scan(doc: pymupdf.Document, page: pymupdf.Page, info: PageInfo) -> None:
    if info.rotation != 0 or info.text_chars:
        return
    if page.cropbox != page.mediabox or page.mediabox.x0 != 0 or page.mediabox.y0 != 0:
        return
    page_area = page.rect.width * page.rect.height
    candidates = []
    for img in info.images:
        if len(img.rects) != 1:
            continue
        rect = pymupdf.Rect(img.rects[0])
        if rect.width * rect.height >= SCAN_MIN_COVERAGE * page_area:
            candidates.append(img)
    if len(candidates) != 1:
        return
    img = candidates[0]
    (_, matrix), = page.get_image_rects(img.xref, transform=True)
    upright = matrix.a > 0 and matrix.d > 0 and abs(matrix.b) < 1e-6 and abs(matrix.c) < 1e-6
    has_mask = bool(_key(doc, img.xref, "SMask")) or _key(doc, img.xref, "ImageMask") == "true"
    if not upright or has_mask or img.bpc not in (1, 8):
        return
    # Any other image drawn visibly on the page could be content (a diagram
    # pasted over the scan); rebuilding would drop it, so stay on redaction.
    for other in info.images:
        if other is not img and other.rects:
            return
    info.scan_xref = img.xref
    info.scan_rect = img.rects[0]


@dataclass
class Raster:
    """One page as pixels, plus how to map a pixel box back to PDF points."""
    image: Image.Image
    # PDF point = origin + pixel * scale, per axis.
    origin: tuple[float, float]
    scale: tuple[float, float]
    from_scan: bool

    def to_pdf_rect(self, box: tuple[int, int, int, int]) -> pymupdf.Rect:
        ox, oy = self.origin
        sx, sy = self.scale
        return pymupdf.Rect(ox + box[0] * sx, oy + box[1] * sy, ox + box[2] * sx, oy + box[3] * sy)


def pixmap_to_image(pix: pymupdf.Pixmap) -> Image.Image:
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    if pix.n not in (1, 3):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    mode = "L" if pix.n == 1 else "RGB"
    return Image.frombytes(mode, (pix.width, pix.height), pix.samples)


def raster_page(doc: pymupdf.Document, info: PageInfo, dpi: int = RENDER_DPI,
                force_render: bool = False) -> Raster:
    if info.is_scan and not force_render:
        img = pixmap_to_image(pymupdf.Pixmap(doc, info.scan_xref))
        x0, y0, x1, y1 = info.scan_rect
        return Raster(img, (x0, y0), ((x1 - x0) / img.width, (y1 - y0) / img.height), True)
    # On a rotated page the pixel boxes do not map onto unrotated PDF
    # coordinates by a plain scale; detect.plan_page sends those to review
    # instead of editing them.
    page = doc[info.number]
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    r = page.rect
    return Raster(pixmap_to_image(pix), (r.x0, r.y0), (r.width / pix.width, r.height / pix.height),
                  False)
