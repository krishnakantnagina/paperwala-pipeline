"""placement.py: headers copied from real RGPV papers, and what the router must do with them."""
from pwpipe import placement as P

BARCH = """Total No. of Questions : 8]
[Total No. of Printed Pages : 2
[1]
Roll No ..................................
AR-227-CBGS
B.Arch. IV Semester
Examination, June 2020
Choice Based Grading System (CBGS)
Ecology and Environment
Time : Three Hours
Maximum Marks : 50"""

CBCS = """Total No. of Questions : 8] [Total No. of Printed Pages : 2
Roll No ..................................
CE-221-CBCS
B.E., II Semester Examination, June 2020
Choice Based Credit System (CBCS)
Fluid Mechanics
Time : Three Hours"""

BPHARM_OCR = """Toral No. of Questions : 8] [Total No. of Printed Pages : 2
ROUNO .verrevreeerncirnansennesssnees .3 a) what are the advantages of the computer
BP-101T (CBGS) eprer ST P gt
B.Pharmacy I Semester (PCI Scheme) il ar
Examination, December 2023 b) Draw the label diagram
Human Anatomy and Physiology I"""

NOISE = "e R ility\n3. a)p What is pest control?\nmarks 7\nl l"


def subj(id, program, sem, code, name, scheme="", branch=""):
    return P.Subject(id, program, branch, sem, code, name, scheme)


def cat(*subjects):
    return P.Catalog(list(subjects))


def test_parse_barch_header():
    f = P.parse_header(BARCH)
    assert (f.program, f.semester, f.code_key, f.scheme, f.month, f.year) == ("B.Arch", 4, "AR227", "CBGS", "June", 2020)
    assert f.title == "Ecology and Environment" and f.complete


def test_parse_be_header_is_btech_cbcs():
    f = P.parse_header(CBCS)
    assert (f.program, f.semester, f.code, f.scheme) == ("B.Tech", 2, "CE-221", "CBCS")
    assert f.title == "Fluid Mechanics"


def test_parse_ocr_header_with_noise_and_suffix_letter_code():
    f = P.parse_header(BPHARM_OCR)
    assert f.program == "B.Pharmacy" and f.semester == 1 and f.code_key == "BP101T" and f.scheme == "CBGS" and f.year == 2023


def test_single_letter_prefix_is_not_a_code():
    assert P.parse_header("P .. . . . e I-121 Total No. of Questions December 2024 something readable here").code_key == ""


def test_unreadable_header():
    assert not P.parse_header("l l").readable
    assert P.decide("l l", subj(1, "B.Arch", 4, "AR-227", "Ecology and Environment"), cat()).kind == "unverified"


def test_keep_when_code_matches():
    s = subj(1, "B.Arch", 4, "AR-227", "Ecology and Environment")
    d = P.decide(BARCH, s, cat(s))
    assert (d.kind, d.reason) == ("keep", "code_match")


def test_move_to_the_subject_the_code_names():
    wrong = subj(1, "B.Tech", 2, "", "Ce Fluid Mechanics", branch="Civil")
    right = subj(2, "B.Tech", 2, "CE-221", "Fluid Mechanics", "CBCS", branch="Civil")
    d = P.decide(CBCS, wrong, cat(wrong, right))
    assert (d.kind, d.target_id) == ("move", 2)


def test_same_code_other_scheme_goes_to_the_sibling():
    cbgs = subj(1, "B.Tech", 2, "CE-221", "Fluid Mechanics", "CBGS", branch="Civil")
    cbcs = subj(2, "B.Tech", 2, "CE-221", "Fluid Mechanics", "CBCS", branch="Civil")
    d = P.decide(CBCS, cbgs, cat(cbgs, cbcs))
    assert (d.kind, d.reason, d.target_id) == ("move", "scheme_sibling", 2)


def test_scheme_conflict_without_a_sibling_is_for_a_person():
    cbgs = subj(1, "B.Tech", 2, "CE-221", "Fluid Mechanics", "CBGS", branch="Civil")
    assert P.decide(CBCS, cbgs, cat(cbgs)).kind == "review"


def test_code_reused_for_another_subject_moves_only_with_a_clear_name_margin():
    filed = subj(1, "B.Arch", 4, "AR-227", "Town Planning")
    other = subj(2, "B.Arch", 4, "AR-227", "Ecology and Environment")
    d = P.decide(BARCH, filed, cat(filed, other))
    assert (d.kind, d.target_id) == ("move", 2)


def test_code_not_on_site_but_name_matches_filed_is_kept():
    s = subj(1, "B.Arch", 4, "AR-226", "Ecology and Environment")
    d = P.decide(BARCH, s, cat(s))
    assert (d.kind, d.reason) == ("keep", "same_name_other_code")


def test_complete_header_for_a_subject_the_site_lacks_proposes_a_new_subject():
    s = subj(1, "B.Arch", 4, "AR-999", "Something Else Entirely")
    d = P.decide(BARCH, s, cat(s))
    assert d.kind == "new_subject"


def test_garbled_title_never_creates_a_subject():
    text = "Total No. of Questions : 8]\nPY-112\nB.Pharmacy I Semester\nExamination, December 2016\nN oride\nTime : 3 hrs"
    s = subj(1, "B.Pharmacy", 1, "PY-104", "Pharmaceutical Chemistry 1")
    assert P.decide(text, s, cat(s)).kind != "new_subject"


def test_two_candidates_for_one_code_need_a_person():
    filed = subj(1, "B.Pharmacy", 1, "BP-103T", "Pharmaceutics 1")
    a = subj(2, "B.Pharmacy", 1, "PY-103", "Pharmaceutical 1")
    b = subj(3, "B.Pharmacy", 1, "PY-103", "Introduction to Pharmaceutics")
    text = "PY-103\nB.Pharmacy I Semester\nExamination, June 2020\nGrading System (GS)\nZzz Unreadable\nTime : 3"
    assert P.decide(text, filed, cat(filed, a, b)).kind == "review"


def test_ocr_semester_misread_does_not_override_a_matching_code_and_name():
    s = subj(1, "B.Pharmacy", 2, "BP-203T", "Bio Chemistry")
    text = "BP-203-T (CBGS)\nB.Pharm I Semester\nExamination, May 2018\nBio-Chemistry\nTime"
    assert P.decide(text, s, cat(s), ocr=True).kind == "keep"
    assert P.decide(text, s, cat(s), ocr=False).kind == "review"


def test_cross_program_header_is_not_kept_silently():
    s = subj(1, "B.Tech", 4, "AR-227", "Ecology and Environment")
    assert P.decide(BARCH, s, cat(s)).kind != "keep"


def test_noise_is_unverified_not_unsorted():
    s = subj(1, "B.Pharmacy", 2, "PY-204", "Pharmacognosy 1")
    assert P.decide(NOISE + "\n" + "readable words here to pass the length gate", s, cat(s)).kind == "unverified"


def test_a_name_that_differs_only_by_its_number_is_a_different_subject():
    text = "PY-505\nB.Pharmacy V Semester\nExamination, June 2020\nGrading System (GS)\nPharmacology - II\nTime : 3"
    filed = subj(1, "B.Pharmacy", 5, "BP-503T", "Pharmacology 2")
    other = subj(2, "B.Pharmacy", 5, "PY-505", "Pharmacology 1")
    assert P.name_seen(text, "Pharmacology 2") >= 0.9 and P.name_seen(text, "Pharmacology 1") < 0.6
    d = P.decide(text, filed, cat(filed, other))
    assert d.kind != "move"


def test_elective_letter_is_part_of_the_code():
    f = P.parse_header("Total No. of Questions : 8]\nBP-803T(A)\nB.Pharmacy VIII Semester\nExamination, June 2025\nPharmacovigilance\nTime : 3")
    assert f.code_key == "BP803TA"
    g = P.parse_header("Total No. of Questions : 8]\nBP-803T-D\nB.Pharmacy VIII Semester\nExamination, June 2025\nScreening Methods\nTime : 3")
    assert g.code_key == "BP803TD"
    assert P.parse_header("BP-101T (CBGS)\nB.Pharmacy I Semester\nExamination, December 2023\nHuman Anatomy\nTime").code_key == "BP101T"


def test_a_bare_elective_bucket_code_never_moves_a_paper():
    bucket = subj(1, "B.Pharmacy", 8, "BP-803T", "G Pharmacovigilance")
    opt_d = subj(2, "B.Pharmacy", 8, "BP-803T-D", "Pharmacological Screening Methods")
    filed = subj(3, "B.Pharmacy", 8, "BP-804T", "Pharmaceutical Marketing Management")
    text = "BP-803-T (CBGS)\nB.Pharmacy VIII Semester\nExamination, June 2020\nTime : Three Hours\nMaximum Marks : 75"
    d = P.decide(text, filed, cat(bucket, opt_d, filed))
    assert (d.kind, d.reason) == ("review", "elective_family")


DIPLOMA_OCR = """$/2013/5146 Total Pages : 6 @
TR F1 B 2
FIFTH SEMESTER (o) digra
ARCHITECTURE & INTERIOR DESIGN (a) R
SPECIFICATION WRITING (@) 3
Time : Three Hours Maximum Marks : 100"""

DIPLOMA_TEXT = """F1202416854
Total Pages : 4
Sixth Semester
Electrical Engineering
Scheme OCBC 2019
INSTALLATION MAINTENANCE AND TESTING
Time: Three Hours"""


def test_diploma_header_gives_paper_code_semester_and_scheme():
    f = P.parse_header(DIPLOMA_OCR)
    assert (f.program, f.code_key, f.semester, f.year) == ("Diploma", "5146", 5, 2013)
    g = P.parse_header(DIPLOMA_TEXT)
    assert (g.program, g.code_key, g.semester, g.scheme) == ("Diploma", "6854", 6, "AICTE")


def test_diploma_paper_is_kept_by_its_paper_code_and_moved_by_it_too():
    right = subj(1, "Diploma", 0, "5146", "Specification Writing")
    wrong = subj(2, "Diploma", 0, "6236", "General Mechanical Engineering")
    assert P.decide(DIPLOMA_OCR, right, cat(right, wrong), ocr=True).reason == "code_match"
    d = P.decide(DIPLOMA_OCR, wrong, cat(right, wrong), ocr=True)
    assert (d.kind, d.target_id) == ("move", 1)
