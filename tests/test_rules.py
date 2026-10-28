import pytest

from pwpipe.blocklist import Blocklist
from pwpipe.ocr import OcrError, parse_tsv
from pwpipe.rules import AD, SUSPECT, WEAK, classify_line, find_domains, find_phones, fix_digits


@pytest.fixture(scope="module")
def bl():
    return Blocklist.load()


def test_fix_digits_only_touches_numberish_tokens():
    assert fix_digits("Whatsapp @ 93009300I2") == "Whatsapp @ 9300930012"
    assert fix_digits("Biology Is fun") == "Biology Is fun"


@pytest.mark.parametrize("text", [
    "Whatsapp @ 93009300I2", "Call 93009 30012", "+91 9300-930012", "mob. 9300930O12",
    "Call 930 093 0012", "+91 98765 43210",
])
def test_phone_found_through_ocr_noise(text):
    assert find_phones(text)


@pytest.mark.parametrize("text", [
    "Paper code MCSE-101", "Roll No. 0123456789", "Max Marks 70 Time 3 hours",
    "Session 2026-2027", "12345 67890",  # starts with 1: not an Indian mobile
    "जनसंख्या (लाखों में) 70 60 38 32 48",   # a table row, not 7060383248
])
def test_no_phone_in_paper_numbers(text):
    assert not find_phones(text)


def test_domains_survive_ocr_spacing():
    assert find_domains("Visit www. pyqonline .com today") == ["pyqonline.com"]


@pytest.mark.parametrize("text", [
    "https://www.pyqonline.com", "www.mpboardonline.corn", "Visit pyqonline.com",
])
def test_blocklisted_domain_is_ad(bl, text):
    assert classify_line(text, bl)[0] == AD


def test_board_name_in_header_is_not_ad(bl):
    # "assamboard" + "of": the label alone must not match.
    assert classify_line("ASSAM BOARD OF SECONDARY EDUCATION", bl)[0] is None
    assert classify_line("Aryabhatta Knowledge University AKU Bihar", bl)[0] is None


def test_official_site_is_never_flagged(bl):
    # "Visit"/"www" may make it weak, which only matters inside an ad block.
    assert classify_line("Visit mpbse.mp.gov.in for results", bl)[0] not in (AD, SUSPECT)
    assert classify_line("www.rgpv.ac.in", bl)[0] not in (AD, SUSPECT)


def test_phone_with_keyword_or_alone_is_ad_in_a_sentence_is_suspect(bl):
    assert classify_line("Whatsapp @ 93009300I2", bl)[0] == AD
    assert classify_line("9300930012", bl)[0] == AD
    assert classify_line("Mob. 93009 30012", bl)[0] == AD
    # A maths question must never be erased automatically.
    assert classify_line("Write 9876543210 in words", bl)[0] == SUSPECT


def test_any_other_written_out_domain_is_ad(bl):
    kind, reason = classify_line("www.someothersite.com", bl)
    assert kind == AD and reason == "domain:someothersite.com"


def test_allowlisted_site_in_a_question_survives(bl):
    assert classify_line("Q3. What is the use of www.google.com?", bl)[0] not in (AD, SUSPECT)
    assert classify_line("Book tickets on irctc.co.in", bl)[0] not in (AD, SUSPECT)


def test_weak_and_plain(bl):
    # An approved line of the pyqonline block since Gate 1.
    assert classify_line("अपने पुराने पेपर्स भेजे और 10 रुपये से 40 रुपये तक पायें", bl)[0] == AD
    assert classify_line("प्रश्न 19 दिए गए संकुल यौगिकों के IUPAC नाम लिखिए", bl)[0] is None


def test_approved_line_matches_fuzzily(bl):
    custom = Blocklist(**{**bl.__dict__, "lines": ["send your old paper get 10 to 40"]})
    assert classify_line("Send your old paper & get L0% to 40%", custom)[0] == AD


def test_blocklist_rejects_unknown_keys(tmp_path):
    path = tmp_path / "bl.json"
    path.write_text('{"domain": []}', encoding="utf-8")
    with pytest.raises(ValueError):
        Blocklist.load(path)


def test_blocklist_version_tracks_content(bl):
    other = Blocklist(**{**bl.__dict__, "lines": ["x" * 20]})
    assert other.version != bl.version


def test_non_tsv_output_is_an_error_not_zero_words():
    with pytest.raises(OcrError):
        parse_tsv("some plain text output\n")


@pytest.mark.parametrize("text", [
    "Tea, coffee etc. in the morning", "Answer as per M.P. in brief", "Collect. In which year",
    "features. India is a country",
])
def test_prose_with_dots_is_not_a_domain(bl, text):
    assert classify_line(text, bl)[0] not in (AD, SUSPECT)


@pytest.mark.parametrize("text", [
    "a2zSUBJCTS.COM Biology", "Visit www. someothersite .com", "http://www.a2zSubjects.com",
])
def test_written_out_unknown_domain_is_ad(bl, text):
    assert classify_line(text, bl)[0] == AD


@pytest.mark.parametrize("text", [
    "Aryabhatta Knowledge University, AKU Bihar in 2019", "Assam Board in the year 2021",
    "MP Board online examination", "DAVV online result",
])
def test_board_name_followed_by_a_word_is_not_its_site(bl, text):
    assert classify_line(text, bl)[0] is None or classify_line(text, bl)[0] == "weak"


@pytest.mark.parametrize("text", [
    "www.akubihar.com", "akubihar .com", "Visit abvmuonline.com", "https://www.aknuonline.com",
    "AKTUONLINE.COM", "wwwmpboardonlinecom",
])
def test_network_domains_are_ads(bl, text):
    assert classify_line(text, bl)[0] == "ad"


@pytest.mark.parametrize("text", [
    "for the attainment of India's independence.In the end", "& ta e.com", "a.b.com is here",
])
def test_prose_and_noise_are_not_promo_domains(bl, text):
    assert classify_line(text, bl)[0] not in (AD, SUSPECT)


@pytest.mark.parametrize("text", [
    "Visit StudyAdda.com for notes", "www.xyz.in", "NOTESHUB.COM", "http://abc.in",
])
def test_real_unlisted_promo_domains_are_still_erased(bl, text):
    assert classify_line(text, bl)[0] == AD


@pytest.mark.parametrize("text", [
    "How do atomic radius vary.in period and in group?", "नाम लिखिए LUP.AC. नाम", "see fig.co for",
])
def test_sentence_endings_are_not_promo_domains(bl, text):
    assert classify_line(text, bl)[0] not in (AD, SUSPECT)


def test_in_domain_with_www_is_still_promo(bl):
    assert classify_line("visit www.examnotes.in", bl)[0] == AD
