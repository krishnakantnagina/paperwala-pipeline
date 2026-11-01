"""End-to-end on real papers. Needs tesseract with the Hindi model; ~1 min each.

Fixtures are two real MP Board papers whose last page carries the pyqonline
"send your old paper" block, and whose every page carries the zero-opacity
A-PDF logo plus a white box over the source's URL footer.
"""

from pathlib import Path

import pymupdf
import pytest

from pwpipe.blocklist import Blocklist
from pwpipe.ocr import OcrError, tesseract_cmd
from pwpipe.process import process_paper

FIXTURES = Path(__file__).parent / "fixtures"

try:
    tesseract_cmd()
    HAVE_TESSERACT = True
except OcrError:
    HAVE_TESSERACT = False

pytestmark = pytest.mark.skipif(not HAVE_TESSERACT, reason="tesseract not installed")


@pytest.fixture(scope="module")
def bl():
    return Blocklist.load()


@pytest.mark.parametrize("name", ["mp_bio_quarterly_2027.pdf", "mp_chem_2027.pdf"])
def test_real_paper_cleaned_and_verified(tmp_path, bl, name):
    src = FIXTURES / name
    dst = tmp_path / name
    report = process_paper(src, dst, bl, crops_dir=tmp_path / "crops")

    assert report["verify"]["ok"], report["verify"]["failures"]
    assert report["auto_ok"] and report["review"] == []
    assert report["page_count"] == 4 and report["pages_dropped"] == 0

    last = report["pages"][3]
    assert len(last["regions"]) == 1
    reasons = " ".join(last["regions"][0]["reasons"])
    assert "pyqonline" in reasons and "whatsapp" in reasons
    assert all(not p["regions"] for p in report["pages"][:3])
    assert all(p["pixels"] == "ok" for p in report["verify"]["pages"])

    out = pymupdf.open(dst)
    assert out.metadata["creator"] == "" and out.metadata["producer"] == ""
    for page in out:
        # Only the scan survives: no hidden logo, no white box - plus the mark.
        assert len(page.get_images()) == 1
        assert page.get_drawings() == []
        # 3 diagonal lines x 2 copies, plus the top and foot stamps where the page has room for them
        words = page.get_text().split()
        assert set(words) == {"paperwala.org"} and 6 <= len(words) <= 8
    assert report["stamped_pages"] == 4 and report["stamps"] == 8
    assert all(p["stamp"] == "ok" for p in report["verify"]["pages"])
    assert dst.stat().st_size <= src.stat().st_size * 1.2
    assert (tmp_path / "crops" / "p4_r1.png").exists()


def test_text_pdf_takes_redaction_path(tmp_path, bl):
    src = tmp_path / "text.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for i in range(1, 15):
        page.insert_text((72, y), f"Q{i}. Explain the working principle of device number {i}.",
                         fontsize=12)
        y += 24
    page.insert_text((180, 720), "Visit www.pyqonline.com", fontsize=14)
    page.insert_text((180, 745), "WhatsApp 9300930012", fontsize=14)
    doc.set_metadata({"creator": "A-PDF Image To PDF"})
    doc.save(src)

    dst = tmp_path / "text_clean.pdf"
    report = process_paper(src, dst, bl)
    assert report["pages"][0]["mode"] == "redact"
    assert report["verify"]["ok"], report["verify"]["failures"]
    text = pymupdf.open(dst)[0].get_text()
    assert "pyqonline" not in text and "9300930012" not in text
    assert "Q14. Explain the working principle" in text


def _scan_with_grey_diagonal(path):
    """A one-page scan: black text lines plus a mid-grey 'RGPVonline.com' across it at 35 degrees."""
    import io
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("L", (1240, 1754), 255)
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=30)
    for i in range(30):
        d.text((100, 120 + i * 50), f"Q{i + 1}. Explain the working principle of device number {i + 1}.",
               fill=20, font=font)
    layer = Image.new("L", img.size, 255)
    ImageDraw.Draw(layer).text((60, 700), "RGPVonline.com", fill=170,
                               font=ImageFont.load_default(size=150))
    layer = layer.rotate(35, fillcolor=255)
    img = Image.fromarray(__import__("numpy").minimum(__import__("numpy").asarray(img),
                                                      __import__("numpy").asarray(layer)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=buf.getvalue())
    doc.save(path)
    return img


def test_grey_diagonal_watermark_is_whitened_and_text_kept(tmp_path, bl):
    import numpy as np
    from PIL import Image
    from pwpipe.pdf import open_pdf, largest_image

    src = tmp_path / "grey.pdf"
    before = _scan_with_grey_diagonal(src)
    dst = tmp_path / "grey_clean.pdf"
    report = process_paper(src, dst, bl, diagonal=False)
    assert report["grey_watermark_pages"] == 1
    assert report["pages"][0]["grey_pixels"] > 1500

    doc = open_pdf(dst)
    after = largest_image(doc, 0).convert("L")
    doc.close()
    from PIL import ImageFilter

    def band(im):                                    # grey pixels away from any ink
        a = np.asarray(im.convert("L"))
        near = np.asarray(Image.fromarray(((a < 100) * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(9)))
        return int(((a >= 130) & (a <= 205) & (near == 0)).sum())
    assert band(after) < band(before) * 0.2          # the grey watermark is gone
    ink = lambda im: int((np.asarray(im) < 100).sum())
    assert ink(after) >= ink(before) * 0.98          # the paper's own text is not


def test_diagonal_marks_added_last(tmp_path, bl):
    src = tmp_path / "text.pdf"
    doc = pymupdf.open()
    for _ in range(2):
        page = doc.new_page()
        for i in range(14):
            page.insert_text((72, 72 + i * 24), f"Q{i}. Explain the working principle of device {i}.",
                             fontsize=12)
    doc.save(src)
    report = process_paper(src, tmp_path / "out.pdf", bl)
    assert report["diagonal_marks"] == 12            # 2 pages x 3 lines x 2 copies
    assert "diagonal_mark_failed" not in report["review"]
    assert report["output_sha256"]
