"""Write the cleaned PDF from a source PDF and its page plans.

Scan pages are rebuilt: a fresh page of the same size carrying only the scan
image, at the same position, with ad boxes painted white. Unchanged scans are
re-inserted from their original bytes, so they lose no quality. Other pages
are copied and edited with redactions, which blank both text and image pixels
under the box.

The source file is only ever read.
"""

from __future__ import annotations

import io
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

from .blocklist import Blocklist
from .detect import PagePlan
from .pdf import PdfInfo, image_sha1, open_pdf, pixmap_to_image
from .rules import AD, classify_line

JPEG_QUALITY = 92


def _insert_flate_image(page: pymupdf.Page, rect: pymupdf.Rect, img: Image.Image,
                        one_bit: bool) -> None:
    """Insert pixels as a FlateDecode image at their true bit depth.

    insert_image() expands a 1-bit scan to 8-bit gray, roughly doubling the
    file. So the image object is written by hand and then placed by xref.
    (Not by swapping the stream of an inserted placeholder: insert_image
    de-duplicates identical images, so every page would share one object.)
    PIL's packed mode-"1" rows (1 = white, rows padded to a byte) are exactly
    PDF DeviceGray at 1 bpc.
    """
    if one_bit:
        img = img.convert("1", dither=Image.Dither.NONE)
        bpc, colorspace = 1, "/DeviceGray"
    elif img.mode == "L":
        bpc, colorspace = 8, "/DeviceGray"
    else:
        img = img.convert("RGB")
        bpc, colorspace = 8, "/DeviceRGB"
    doc = page.parent
    xref = doc.get_new_xref()
    doc.update_object(xref, (
        f"<< /Type /XObject /Subtype /Image /Width {img.width} /Height {img.height} "
        f"/BitsPerComponent {bpc} /ColorSpace {colorspace} >>"))
    # Raw samples in; update_stream deflates them and adds the /Filter itself.
    # (Pre-compressing with zlib got deflated a second time.)
    doc.update_stream(xref, img.tobytes(), new=True)
    page.insert_image(rect, xref=xref, keep_proportion=False)


def _rebuild_page(src: pymupdf.Document, out: pymupdf.Document, info, plan: PagePlan) -> None:
    page = out.new_page(width=info.width, height=info.height)
    rect = pymupdf.Rect(info.scan_rect)
    image = next(i for i in info.images if i.xref == info.scan_xref)
    regions = plan.applied_regions
    is_jpeg = "DCT" in image.filter

    if is_jpeg and not regions:
        # Original JPEG bytes pass through untouched: no generation loss.
        page.insert_image(rect, stream=src.extract_image(info.scan_xref)["image"],
                          keep_proportion=False)
        return

    img = pixmap_to_image(pymupdf.Pixmap(src, info.scan_xref))
    if regions:
        draw = ImageDraw.Draw(img)
        white = 255 if img.mode == "L" else (255, 255, 255)
        for region in regions:
            x0, y0, x1, y1 = region.bbox
            draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=white)
    if is_jpeg:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=JPEG_QUALITY)
        page.insert_image(rect, stream=buf.getvalue(), keep_proportion=False)
    else:
        _insert_flate_image(page, rect, img, one_bit=image.bpc == 1)


def _redact_page(src: pymupdf.Document, out: pymupdf.Document, plan: PagePlan,
                 bl: Blocklist) -> None:
    out.insert_pdf(src, from_page=plan.number, to_page=plan.number)
    page = out[-1]

    if plan.delete_image_sha1:
        for item in page.get_images(full=True):
            if image_sha1(out, item[0]) in plan.delete_image_sha1:
                page.delete_image(item[0])

    for link in page.get_links():
        uri = link.get("uri") or ""
        if uri and classify_line(uri, bl)[0] == AD:
            page.delete_link(link)

    regions = plan.applied_regions
    if not regions:
        return
    ox, oy = plan.origin
    sx, sy = plan.scale
    for region in regions:
        x0, y0, x1, y1 = region.bbox
        rect = pymupdf.Rect(ox + x0 * sx, oy + y0 * sy, ox + x1 * sx, oy + y1 * sy)
        page.add_redact_annot(rect, fill=(1, 1, 1))
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)


def clean_pdf(src_path: str | Path, info: PdfInfo, plans: list[PagePlan], bl: Blocklist,
              dst_path: str | Path) -> None:
    src = open_pdf(src_path)
    out = pymupdf.open()
    try:
        for page_info, plan in zip(info.pages, plans, strict=True):
            if plan.drop:
                continue
            if plan.mode == "rebuild":
                _rebuild_page(src, out, page_info, plan)
            else:
                _redact_page(src, out, plan, bl)
        if out.page_count == 0:
            raise ValueError("every page planned as dropped; refusing to write an empty PDF")
        # The originals carry the converter's name ("A-PDF Image To PDF") and
        # XMP from the source site; none of it is ours to republish.
        out.set_metadata({})
        out.del_xml_metadata()
        tmp = Path(str(dst_path) + ".part")
        out.save(tmp, garbage=4, deflate=True, clean=True)
    finally:
        out.close()
        src.close()
    # Written to a side file then renamed, so a crash never leaves a
    # half-written PDF under the real name.
    tmp.replace(dst_path)
