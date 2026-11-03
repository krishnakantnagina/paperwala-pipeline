"""Rules from the 54-paper AKTU cleaning sample (2026-11-04, local/aktu_clean_sample/REPORT.md)."""

import pymupdf
import pytest

from pwpipe import ruled
from pwpipe.blocklist import Blocklist
from pwpipe.ocr import OcrError
from pwpipe.rules import AD, classify_line
from pwpipe.stamp import add_diagonal_marks, draw_stamp

@pytest.fixture(scope="module")
def bl():
    return Blocklist.load()


@pytest.mark.parametrize("text", [
    "influencing ||. hUps:/iwww.aktuoniine com",       # OCR of a small URL inside a 2024-25 question table
    "https://www.aktu0nl1ne.com",
    "uptuontine.com",                                   # the old name of aktuonline, OCR'd
])
def test_ocr_misread_aktu_urls_are_ads_and_url_words_only(bl, text):
    kind, reason = classify_line(text, bl)
    assert kind == AD and reason.startswith("domain:")


@pytest.mark.parametrize("text", ["aktu question papers visit", "For AKTU Question Papers Visit"])
def test_left_over_visit_footer_is_an_ad(bl, text):
    # The earlier cleaner took the URL off the end and left the lead-in (M.Tech 2013 paper).
    assert classify_line(text, bl)[0] == AD


@pytest.mark.parametrize("text", [
    "What do you mean by www?", "Define online consumer behavior?", "(c) Mobile Satellite Communication",
    "Discuss the online lining of a wall", "Write short notes on question papers",
])
def test_question_text_is_still_not_an_ad(bl, text):
    assert classify_line(text, bl)[0] != AD


def test_a_strip_tesseract_crashes_on_is_skipped_not_fatal(monkeypatch, bl):
    from PIL import Image, ImageDraw
    img = Image.new("L", (1000, 600), 255)
    ImageDraw.Draw(img).line((50, 300, 950, 300), fill=0, width=3)
    calls = []

    def boom(strip, langs, psm=3, **kw):
        calls.append(langs)
        raise OcrError("")                 # tesseract 5.5 exit 139, empty stderr
    monkeypatch.setattr(ruled, "ocr_image", boom)
    assert ruled.struck_through_ads(img, bl) == []
    assert calls[-1] == "eng"              # retried in English before giving up


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_marks_land_upright_where_planned_on_a_rotated_page(tmp_path, rotation):
    import numpy as np
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=600)
    page.set_rotation(rotation)
    w, h = page.rect.width, page.rect.height          # the page as displayed
    rect = pymupdf.Rect(w * 0.35, h * 0.01, w * 0.65, h * 0.04)
    draw_stamp(page, rect)
    path = tmp_path / "rot.pdf"
    doc.save(path)
    pix = pymupdf.open(path)[0].get_pixmap(dpi=72, colorspace=pymupdf.csGRAY)
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width)
    ys, xs = np.nonzero(a < 200)
    assert xs.size, "mark not on the page"
    # inside the planned box and wider than tall, i.e. reading upright
    assert rect.x0 - 2 <= xs.min() and xs.max() <= rect.x1 + 2
    assert rect.y0 - 2 <= ys.min() and ys.max() <= rect.y1 + 2
    assert xs.max() - xs.min() > 3 * (ys.max() - ys.min())
    assert add_diagonal_marks(path) >= 6
