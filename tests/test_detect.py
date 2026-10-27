"""Planner tests on hand-built OCR lines: no tesseract, fully deterministic."""

import pytest

from pwpipe.blocklist import Blocklist
from pwpipe.detect import OcrLine, classify_line, plan_page
from pwpipe.pdf import PageInfo

PAGE = PageInfo(number=0, width=595, height=842, rotation=0, text_chars=0,
                scan_xref=1, scan_rect=(0, 0, 595, 842))
SIZE = (2000, 2800)


@pytest.fixture(scope="module")
def bl():
    return Blocklist.load()


def line(text, y, x0=200, x1=900, bl=None, conf=90):
    kind, reason = classify_line(text, bl) if bl else (None, "")
    words, x = [], x0
    step = (x1 - x0) // max(len(text.split()), 1)
    for w in text.split():
        words.append((w, conf, (x, y, x + step - 10, y + 40)))
        x += step
    return OcrLine(text, (x0, y, x1, y + 40), conf, kind, reason, words)


def questions(bl, n=12, start=200):
    return [line(f"Q{i}. Explain the process number {i} with a neat diagram", start + i * 60,
                 bl=bl) for i in range(n)]


def ad_block(bl, y=1500):
    return [
        line("https://www.pyqonline.com", y, bl=bl),
        line("Whatsapp @ 93009300I2", y + 55, bl=bl),
        line("Send your old paper & get 10 to 40", y + 110, bl=bl),
        line("अपने पुराने पेपर्स भेजे और 10 रुपये से 40 रुपये तक पायें,", y + 165, bl=bl),
        line("Paytm or Google Pay से", y + 220, bl=bl),
    ]


def test_ad_block_found_whole_and_safe(bl):
    plan = plan_page(PAGE, SIZE, questions(bl) + ad_block(bl), bl)
    assert len(plan.regions) == 1
    region = plan.regions[0]
    assert region.safe and len(region.lines) == 5
    assert plan.review == [] and not plan.drop


def test_ad_touching_question_is_conflict_not_erased(bl):
    lines = questions(bl, n=12, start=200)
    # Ad pressed right under the last question, overlapping its box.
    lines += ad_block(bl, y=lines[-1].bbox[1] + 20)
    plan = plan_page(PAGE, SIZE, lines, bl)
    assert plan.regions and not plan.regions[0].safe
    assert "conflict" in plan.review
    assert plan.applied_regions == []


def test_url_stamped_beside_question_erases_only_url_words(bl):
    # The pilot case: "sound wave of frequency 20 Hz. https://www.mpboardonline.com"
    q = line("Q7. A sound wave of frequency 20 Hz travels through air at room "
             "temperature https://www.mpboardonline.com", 1000, bl=bl)
    plan = plan_page(PAGE, SIZE, questions(bl) + [q], bl)
    assert [r.kind for r in plan.applied_regions] == ["words"]
    assert plan.applied_regions[0].lines == ["https://www.mpboardonline.com"]
    kept = [t for t, _, b in q.words if not plan.erased(b)]
    assert "Q7." in kept and "temperature" in kept and len(kept) == len(q.words) - 1
    assert "conflict" not in plan.review


def test_url_words_touching_another_line_go_to_review(bl):
    # Overlapping the question above (it spans y 860-900): refused.
    q = line("Q7. Read the passage published at www.pyqonline.com and answer the "
             "following questions about photosynthesis in detail", 880, bl=bl)
    plan = plan_page(PAGE, SIZE, questions(bl) + [q], bl)
    assert plan.applied_regions == [] and "conflict" in plan.review


def test_long_line_with_phone_is_never_narrowed(bl):
    q = line("Q7. For help with this chapter call our WhatsApp helpline 9300930012 any "
             "time and explain the process of photosynthesis", 1000, bl=bl)
    plan = plan_page(PAGE, SIZE, questions(bl) + [q], bl)
    assert plan.applied_regions == [] and "conflict" in plan.review


def test_page_of_only_ads_is_dropped_with_review(bl):
    plan = plan_page(PAGE, SIZE, ad_block(bl, y=300), bl)
    assert plan.drop and "drop_page" in plan.review


def test_bare_phone_goes_to_review(bl):
    plan = plan_page(PAGE, SIZE, questions(bl) + [line("9300930012", 1600, bl=bl)], bl)
    assert plan.regions == [] and "suspect" in plan.review


def test_blank_page_goes_to_review(bl):
    plan = plan_page(PAGE, SIZE, [line("..", 400, bl=bl)], bl)
    assert "low_text" in plan.review


def test_clean_paper_needs_nothing(bl):
    plan = plan_page(PAGE, SIZE, questions(bl), bl)
    assert plan.regions == [] and plan.review == []


def test_erased_content_catches_a_plan_that_erases_a_question(bl):
    from pwpipe.detect import Region
    from pwpipe.rules import _domain_label
    from pwpipe.verify import erased_content

    labels = [_domain_label(d) for d in bl.domains]
    qs = questions(bl)
    plan = plan_page(PAGE, SIZE, qs + ad_block(bl), bl)
    assert erased_content(plan, labels) == []          # the real ad block is all ad

    plan.regions.append(Region(bbox=qs[3].bbox, lines=[qs[3].text], reasons=["bug"]))
    assert "Explain" in erased_content(plan, labels)


def test_erased_content_catches_whole_url_line_erasure(bl):
    from pwpipe.detect import Region
    from pwpipe.rules import _domain_label
    from pwpipe.verify import erased_content

    labels = [_domain_label(d) for d in bl.domains]
    q = line("bee loud glade. https:/www.mpboardonline", 1000, bl=bl)
    plan = plan_page(PAGE, SIZE, questions(bl) + [q], bl)
    assert erased_content(plan, labels) == []          # only the URL word is erased
    plan.regions = [Region(bbox=q.bbox, lines=[q.text], reasons=["old behaviour"])]
    assert set(erased_content(plan, labels)) >= {"bee", "loud"}
