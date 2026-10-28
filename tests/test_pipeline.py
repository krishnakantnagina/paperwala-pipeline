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
        assert page.get_text().strip() == "paperwala.org"
    assert report["stamped_pages"] == 4
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
