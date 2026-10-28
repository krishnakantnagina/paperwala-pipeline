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


def test_ad_touching_question_is_erased_anyway(bl):
    # Owner's rule (2026-10-28): an identified ad goes even if it clips the
    # question it touches; exact boxes, never padded; recorded, not reviewed.
    lines = questions(bl, n=12, start=200)
    lines += ad_block(bl, y=lines[-1].bbox[1] + 20)    # overlapping the last question
    plan = plan_page(PAGE, SIZE, lines, bl)
    (region,) = plan.applied_regions
    assert region.forced and region.clipped >= 1
    assert "conflict" not in plan.review
    ad_top = min(l.bbox[1] for l in lines[-5:])
    assert all(b[1] >= ad_top - 20 for b in region.boxes)   # nothing above the ad (pad <= 16)


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


def test_url_words_touching_another_line_are_erased_exactly(bl):
    # Overlapping the question above (it spans y 860-900): only the URL
    # word's own box goes, the rest of the line stays.
    q = line("Q7. Read the passage published at www.pyqonline.com and answer the "
             "following questions about photosynthesis in detail", 880, bl=bl)
    plan = plan_page(PAGE, SIZE, questions(bl) + [q], bl)
    (region,) = plan.applied_regions
    url_box = next(b for t, _, b in q.words if "pyqonline" in t)
    assert region.forced and region.boxes == [url_box]


def test_long_line_with_phone_is_never_narrowed(bl):
    q = line("Q7. For help with this chapter call our WhatsApp helpline 9300930012 any "
             "time and explain the process of photosynthesis", 1000, bl=bl)
    plan = plan_page(PAGE, SIZE, questions(bl) + [q], bl)
    assert plan.applied_regions == [] and "conflict" in plan.review


def test_page_of_only_ads_is_dropped_with_review(bl):
    plan = plan_page(PAGE, SIZE, ad_block(bl, y=300), bl)
    assert plan.drop and "drop_page" in plan.review


def test_bare_phone_is_erased(bl):
    plan = plan_page(PAGE, SIZE, questions(bl) + [line("Mob. 9300930012", 1600, bl=bl)], bl)
    assert len(plan.applied_regions) == 1 and plan.review == []


def test_phone_inside_a_question_goes_to_review(bl):
    q = line("Q9. Write the number 9876543210 in words", 1600, bl=bl)
    plan = plan_page(PAGE, SIZE, questions(bl) + [q], bl)
    assert plan.regions == [] and "suspect" in plan.review


def test_other_promo_site_beside_question_loses_only_its_url(bl):
    q = line("सही विकल्प चुनिये - a2zSUBJCTS.COM", 1600, bl=bl)
    plan = plan_page(PAGE, SIZE, questions(bl) + [q], bl)
    assert [r.lines for r in plan.applied_regions] == [["a2zSUBJCTS.COM"]]
    kept = [t for t, _, b in q.words if not plan.erased(b)]
    assert kept == ["सही", "विकल्प", "चुनिये", "-"]


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


def test_url_line_above_phone_line_erases_exact_parts(bl):
    # "Visit" stays; the URL words and the whole WhatsApp line go.
    url = line("Visit www.pyqonline.com", 1500, x0=200, x1=700, bl=bl)
    phone = line("WhatsApp 9300930012", 1555, x0=200, x1=600, bl=bl)
    plan = plan_page(PAGE, SIZE, questions(bl) + [url, phone], bl)
    assert len(plan.applied_regions) == 1
    region = plan.applied_regions[0]
    assert len(region.boxes) == 2 and plan.review == []
    assert not plan.erased(url.words[0][2])          # "Visit"
    assert plan.erased(url.words[1][2]) and all(plan.erased(b) for _, _, b in phone.words)


def test_ad_block_with_garbled_url_is_still_erased(bl):
    # Gate 1, RGPV: the pyqonline URL line was unreadable, the rest remained.
    block = [
        line("Whatsapp @ 930093002", 1500, bl=bl),
        line("Your old paper & get 0/-", 1555, bl=bl),
        line("पुराने पेपर्स भेजे और 0 रुपये पायें,", 1610, bl=bl),
        line("Paytm or Google Pay से", 1665, bl=bl),
    ]
    plan = plan_page(PAGE, SIZE, questions(bl) + block, bl)
    assert len(plan.applied_regions) == 1 and plan.review == []
    assert all(plan.erased(b) for l in block for _, _, b in l.words)


def test_mcq_options_naming_apps_are_not_an_ad(bl):
    opts = [line("(a) Email (b) Fax", 1500, bl=bl), line("(c) Telegram (d) WhatsApp", 1555, bl=bl)]
    plan = plan_page(PAGE, SIZE, questions(bl) + opts, bl)
    assert plan.applied_regions == []


def test_url_between_hindi_lines_is_trimmed_off_their_matras(bl):
    # MP Board Accountancy, sent back by the owner: the stamped URL sits
    # between two lines whose matras reach 3-6 px into its box.
    above = line("Q8. Explain the cash book entries for the cheque", 760, x0=200, x1=900)
    url = line("https://www.mpboardonline.com", 795, x0=400, x1=700, bl=bl)
    below = line("Q9. Write the bank reconciliation statement here", 829, x0=200, x1=900)
    plan = plan_page(PAGE, SIZE, questions(bl, n=8) + [above, url, below], bl)
    (region,) = plan.applied_regions
    (box,) = region.boxes
    assert box[1] > 800 and box[3] < 829 and box[3] - box[1] >= 0.6 * 40


def test_url_beside_a_word_on_its_own_line_is_erased_exactly(bl):
    # A neighbour on the same line is not trimmed around; the exact URL box
    # is erased (owner's rule) and may clip it.
    q = line("Q7. Name it www.pyqonline.com", 880, x0=200, x1=600, bl=bl)
    beside = line("photosynthesis", 885, x0=590, x1=800)
    plan = plan_page(PAGE, SIZE, questions(bl) + [q, beside], bl)
    (region,) = plan.applied_regions
    assert region.forced and region.boxes[0][2] <= 600


def test_url_found_by_another_reading_replaces_its_garbled_shadow(bl):
    from pwpipe.detect import merge_found_ads
    shadow = line("वर्ग अन्तर MPboardon1ine.c0m", 920, x0=160, x1=1180)
    lines = questions(bl) + [shadow]
    found = line("MPboardonline.com", 920, x0=850, x1=1180, bl=bl)
    assert merge_found_ads(lines, [found], bl) == 1
    texts = [l.text for l in lines]
    assert "MPboardonline.com" in texts and not any("MPboardon1ine" in t for t in texts)
    assert "वर्ग अन्तर" in texts              # the question's words stay
    plan = plan_page(PAGE, SIZE, lines, bl)
    assert len(plan.applied_regions) == 1 and plan.review == []
    assert merge_found_ads(lines, [found], bl) == 0   # known now: not added twice


def test_site_name_split_in_two_is_erased_whole(bl):
    # Assam maths paper: "assamboard.com" read as "...assam" + "board.com".
    q = line("are equal. httpsi//www.assam board.com", 1100, x0=130, x1=730, bl=bl)
    plan = plan_page(PAGE, SIZE, questions(bl) + [q], bl)
    (region,) = plan.applied_regions
    assert region.lines == ["httpsi//www.assam board.com"]
    assert region.boxes[0][0] > 280                    # "are equal." stays


def test_garbled_middle_of_a_url_is_part_of_it(bl):
    # BU: a vertical URL read as "http" + "\EEE." + "onlinebu.com".
    q = line('http "\\EEE. onlinebu.com', 1100, x0=600, x1=900, bl=bl)
    plan = plan_page(PAGE, SIZE, questions(bl) + [q], bl)
    (region,) = plan.applied_regions
    assert region.lines == ['http "\\EEE. onlinebu.com']


def test_garbled_word_is_not_taken_without_url_words_both_sides(bl):
    q = line("Q13. Define EEE. www.pyqonline.com", 1100, x0=130, x1=730, bl=bl)
    plan = plan_page(PAGE, SIZE, questions(bl) + [q], bl)
    (region,) = plan.applied_regions
    assert region.lines == ["www.pyqonline.com"]


def test_ink_smear_boxed_as_a_word_does_not_block_a_url(bl):
    # MP Board: OCR boxed a 464 px smear as one word overlapping the URL.
    url = line("https://www.mpboardonline.com", 2263, x0=551, x1=1125, bl=bl)
    smear = OcrLine("रैक", (106, 1859, 570, 2323), 57, None, "", [("रैक", 57, (106, 1859, 570, 2323))])
    plan = plan_page(PAGE, SIZE, questions(bl) + [smear, url], bl)
    assert len(plan.applied_regions) == 1 and "conflict" not in plan.review
