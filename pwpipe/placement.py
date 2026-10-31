"""Decide where a paper belongs from what the paper itself says.

`route.py` asked "does the filed subject's name appear in the header?" and could
only propose a move to a subject the site already has. This module reads the whole
identity block (programme, semester, paper code, scheme, session, subject) and
returns one of six outcomes, so that nothing is moved on a guess and nothing that
cannot be placed stays on a public page:

    keep          the header agrees with where the paper is filed
    unverified    nothing in the header to compare (unreadable, no code, no name): leave it where it is
    move          the header names exactly one other subject the site has: safe to move
    new_subject   the header is complete and the site has no such subject: needs one created
    review        some signals, not enough (two candidates, name only, code reused): a person decides
    unsorted      cannot be placed (unreadable, contradictory, nothing matches): off the site, kept in the database

Pure functions over plain data. No network, no database: the caller loads the
subject catalogue once per run (see `Catalog`) and gives the header text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

# ---------------------------------------------------------------- parsing

ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}
ORDINAL = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
           "ninth": 9, "tenth": 10}
MONTHS = {"jan": "January", "feb": "February", "mar": "March", "apr": "April", "may": "May", "jun": "June",
          "jul": "July", "aug": "August", "sep": "September", "oct": "October", "nov": "November", "dec": "December"}

# header programme text -> the site's program name
PROGRAMS = [
    (r"\bM\.?\s?Pharm", "M.Pharmacy"), (r"\bB\.?\s?Pharm", "B.Pharmacy"), (r"\bD\.?\s?Pharm", "D.Pharmacy"),
    (r"\bM\.?\s?Arch", "M.Arch"), (r"\bB\.?\s?Arch", "B.Arch"),
    (r"\bM\.?\s?Tech", "M.Tech"), (r"\bM\.?\s?E\.?,?(?=\s+[IVX]+\s*Sem|\s+Sem)", "M.Tech"),
    (r"\bB\.?\s?E\.?,?(?=\s+[IVX]+\s*[- ]?\s*Sem|\s+Sem)", "B.Tech"), (r"\bB\.?\s?Tech", "B.Tech"),
    (r"\bM\.?\s?C\.?\s?A\b", "MCA"), (r"\bMBA\b", "MBA"), (r"\bM\.?\s?A\.?\s?M\b", "MAM"),
    (r"\bDiploma\b", "Diploma"),
]

# scheme words printed in the header -> Subject.scheme
SCHEME_WORDS = [
    (r"OCBC\s*(?:20\d\d)?", "AICTE"), (r"CGPA", "GRADING"),
    (r"choice\s+based\s+grading", "CBGS"), (r"\bCBGS\b", "CBGS"),
    (r"choice\s+based\s+credit", "CBCS"), (r"\bCBCS\b", "CBCS"),
    (r"non[- ]?grading", "NONGRADING"), (r"\bNGS\b", "NONGRADING"),
    (r"grading\s+system", "GRADING"), (r"\(\s*GS\s*\)|-GS\b", "GRADING"),
]

_DIPLOMA_CODE = re.compile(r"((?:19|20)\d{2})\W{0,3}[1lI]?\W{0,3}(\d{4,5})\W{0,6}Total\W*Pages", re.I)

_NOT_A_PREFIX = {"JULY", "JUNE", "JAN", "FEB", "MARCH", "APRIL", "MAY", "AUG", "SEPT", "OCT", "NOV", "DEC", "OCBC", "CGPA",
                 "SCHEME", "YEAR", "PAGES", "TIME", "MARKS", "NOTE", "TOTAL", "MAX", "WWW", "HTTP", "ROLL", "SEM"}

_NOT_A_CODE_LINE = re.compile(r"marks|pages|questions|time\s*:|note\s*:|roll\s*no|enrol", re.I)
_CODE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,6})[ -]?(\d{3,4})(?:[ -]?\(?([A-Z])\)?(?![a-z]))?(?:\s*(?:\(\s*([A-E])\s*\)|-\s*([A-E])(?![A-Za-z0-9])))?(?:\s*[-(]\s*(CBCS|CBGS|GS|NGS)\s*\)?)?(?![A-Za-z0-9])")


@dataclass
class HeaderFacts:
    program: str = ""
    semester: int | None = None
    code: str = ""            # "CE-503-A" as printed, normalised
    code_key: str = ""        # "CE503A"
    scheme: str = ""          # CBGS | CBCS | GRADING | NONGRADING | ""
    month: str = ""
    year: int | None = None
    title: str = ""           # the subject name line
    readable: bool = False    # enough of the block was read to decide anything

    @property
    def complete(self) -> bool:
        return bool(self.program and self.semester and self.code_key and self.title)


def code_key(code: str) -> str:
    """CE-503-A, CE503(A) and Ce 503a all give CE503A."""
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def norm(text: str) -> str:
    text = (text or "").lower().replace("&", " and ")
    text = re.sub(r"\b(engg|engg\.)\b", "engineering", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    roman = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6"}
    return " ".join(roman.get(w, w) for w in text.split() if w not in ("and", "of", "the", "for", "in"))


def similarity(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def parse_header(text: str) -> HeaderFacts:
    f = HeaderFacts()
    text = text or ""
    if sum(c.isalnum() for c in text) < 25:
        return f
    f.readable = True
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    head = "\n".join(lines[:16])

    diploma = _DIPLOMA_CODE.search("\n".join(lines[:5]))
    if diploma:
        f.program = "D.Pharmacy" if re.search(r"pharmac", head, re.I) else "Diploma"
        f.year = int(diploma.group(1))
        f.code = f.code_key = diploma.group(2)
    else:
        for pat, program in PROGRAMS:
            if re.search(pat, head, re.I):
                f.program = program
                break

    m = re.search(r"\b(VIII|VII|VI|IV|V|III|II|IX|X|I)\b\s*[-.]?\s*(?:Semester|Sem\b)", head)
    if m:
        f.semester = ROMAN[m.group(1)]
    else:
        m = re.search(r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth)\s*sem", head, re.I)
        if m:
            f.semester = ORDINAL[m.group(1).lower()]
        else:
            m = re.search(r"Semester\s*[-:]?\s*(VIII|VII|VI|IV|V|III|II|IX|X|I)\b", head)
            if m:
                f.semester = ROMAN[m.group(1)]

    if f.program == "D.Pharmacy" and f.semester is None:
        m = re.search(r"\b(first|second)\s*year", head, re.I)
        if m:
            f.semester = ORDINAL[m.group(1).lower()]

    for pat, scheme in SCHEME_WORDS:
        if re.search(pat, head, re.I if pat[0].islower() or pat.startswith("(?") else 0):
            f.scheme = scheme
            break

    # the code: first code-looking token on a line that is not marks/pages/roll
    for ln in ([] if diploma else lines[:14]):
        if _NOT_A_CODE_LINE.search(ln):
            continue
        for m in _CODE.finditer(ln):
            prefix, digits, suffix, elective, elective2, sch = m.groups()
            if suffix == "N":
                suffix = ""
            if prefix in _NOT_A_PREFIX or (prefix in ("PH", "MM") and not suffix and len(ln) > 30):
                continue
            f.code = f"{prefix}-{digits}" + (f"-{suffix}" if suffix else "") + (f"-{elective or elective2}" if (elective or elective2) else "")
            f.code_key = code_key(f.code)
            if sch and not f.scheme:
                f.scheme = {"GS": "GRADING", "NGS": "NONGRADING"}.get(sch, sch)
            break
        if f.code:
            break

    m = re.search(r"(?:Examination|Exam\.?)[\s,.\-]*(?:\(?[A-Za-z. ]*\)?[\s,.\-]*)?"
                  r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?[\s,.\-]*(\d{4})", head, re.I)
    if m:
        f.month = MONTHS[m.group(1).lower()[:3]]
        f.year = f.year or int(m.group(2))

    # the subject: the first real line after the session / scheme lines
    start = 0
    for i, ln in enumerate(lines[:16]):
        if re.search(r"examination|exam\b|semester", ln, re.I) and re.search(r"\d{4}", ln):
            start = i + 1
    for ln in lines[start:start + 6]:
        if re.search(r"grading|credit|system|scheme|\(cb|\bgs\b|time\s*:|maximum|marks|note|attempt|answer|^\W*$", ln, re.I):
            continue
        letters = re.sub(r"[^A-Za-z ]", "", ln)
        if len(letters.strip()) >= 4 and len(ln) <= 90:
            f.title = re.sub(r"\s+", " ", ln).strip(" .:-")
            break
    return f


def name_seen(text: str, name: str, words: list[str] | None = None) -> float:
    """How well `name` appears anywhere in the header text (0..1). Two-column scans put the
    questions beside the title, so the title cannot be cut out reliably; the name can be found.
    `words` is norm(text).split() when the caller already has it (it is the same for every candidate)."""
    n = norm(name)
    if len(n) < 4:
        return 0.0
    if words is None:
        words = norm(text).split()
    k = len(n.split())
    numbers = re.findall(r"\d+", n)
    best = 0.0
    for size in {max(1, k - 1), k, k + 1}:
        for i in range(max(1, len(words) - size + 1)):
            window = " ".join(words[i:i + size])
            sm = SequenceMatcher(None, n, window)
            if sm.real_quick_ratio() <= best or sm.quick_ratio() <= best:
                continue
            r = sm.ratio()
            if re.findall(r"\d+", window) != numbers:
                r = min(r, 0.55)        # "Pharmacology 2" is not "Pharmacology 1"
            if r > best:
                best = r
                if best >= 0.98:
                    return best
    return best


def clean_title(title: str) -> bool:
    """A subject-name-looking line: short, mostly letters, not a sentence or an exam question."""
    t = (title or "").strip()
    if not t or len(t.split()) > 9 or "?" in t or re.search(r"[|{}\\]", t):
        return False
    words = re.findall(r"[A-Za-z]+", t)
    if not any(len(w) >= 4 for w in words):
        return False
    if any(len(w) == 1 and w.upper() not in ("I", "V", "X", "A") for w in words):    # "N oride": a torn word
        return False
    letters = sum(c.isalpha() or c in " -&()." for c in t)
    return letters / len(t) >= 0.88 and t[0].isupper()


# ---------------------------------------------------------------- catalogue

@dataclass
class Subject:
    id: int
    program: str
    branch: str
    semester: int
    code: str
    name: str
    scheme: str = ""

    @property
    def key(self) -> str:
        return code_key(self.code)


class Catalog:
    """The site's subjects, loaded once per run and indexed in memory (one read,
    never one query per paper)."""

    def __init__(self, subjects: list[Subject]):
        self.by_id = {s.id: s for s in subjects}
        self.by_code: dict[tuple[str, str], list[Subject]] = {}
        for s in subjects:
            if s.key:
                self.by_code.setdefault((s.program, s.key), []).append(s)

    def with_code(self, program: str, key: str) -> list[Subject]:
        found = self.by_code.get((program, key), [])
        if not found:
            m = re.fullmatch(r"[A-Z]{2,6}(\d{3,4})", key)
            if m:           # the site stores "703" where the paper prints "MAM-703"
                found = self.by_code.get((program, m.group(1)), [])
        return found

    def family(self, program: str, key: str) -> list[Subject]:
        """Subjects whose code is `key` plus more letters: BP803T is the elective bucket of BP803TA..D."""
        out = []
        for (prog, k), subs in self.by_code.items():
            if prog == program and len(k) > len(key) and k.startswith(key) and k[len(key):].isalpha():
                out += subs
        return out


# ---------------------------------------------------------------- decision

@dataclass
class Decision:
    kind: str                       # keep | move | new_subject | review | unsorted
    target_id: int | None = None    # for move
    reason: str = ""                # short machine-readable cause
    notes: list[str] = field(default_factory=list)
    facts: HeaderFacts | None = None


_FAMILIES = ({"MBA", "MAM"}, {"Diploma", "D.Pharmacy"})

NAME_OK = 0.60                      # header title vs subject name: same subject
NAME_STRONG = 0.80


# What a printed scheme word allows the subject's scheme to be. "Grading System (GS)" is printed on CBGS-era papers
# as well, so the generic word agrees with any grading scheme; only credit-based vs grading (or an explicit CBGS
# against non-grading) is a real contradiction.
_COMPATIBLE = {
    "CBCS": {"CBCS"},
    "CBGS": {"CBGS", "GRADING"},
    "GRADING": {"CBGS", "GRADING", "NONGRADING"},
    "NONGRADING": {"NONGRADING", "GRADING"},
}


def _scheme_conflict(header: str, subject: Subject) -> bool:
    """The printed scheme contradicts the subject's. AICTE has no printed word, so it never conflicts."""
    if not header or not subject.scheme or subject.scheme == "AICTE":
        return False
    return subject.scheme not in _COMPATIBLE.get(header, {header})


def _semester_ok(header: int | None, subject: Subject) -> bool:
    return header is None or subject.semester in (0, header)


def decide(text: str, filed: Subject, catalog: Catalog, ocr: bool = False) -> Decision:
    """`ocr` says the header came from OCR: its semester and title are less reliable than a text layer."""
    f = parse_header(text)
    if not f.readable:
        return Decision("unverified", None, "unreadable", ["header could not be read"], f)
    notes: list[str] = []
    cross = bool(f.program and f.program != filed.program and {f.program, filed.program} not in _FAMILIES)
    program = f.program if cross else filed.program
    if ocr and cross and f.code_key and not catalog.with_code(f.program, f.code_key) and catalog.with_code(filed.program, f.code_key):
        cross, program = False, filed.program      # the printed programme was misread; the code lives in the filed one
    if cross:
        notes.append(f"header says {f.program}, filed under {filed.program}")

    sem_ok = _semester_ok(f.semester, filed)
    words = norm(text).split()
    wset = set(words)
    filed_seen = name_seen(text, filed.name, words)
    title_ok = clean_title(f.title)

    # ---- 1. the paper's code is the filed subject's code
    code_equal = bool(f.code_key) and (f.code_key == filed.key or (
        filed.key.isdigit() and re.fullmatch(r"[A-Z]{2,6}" + filed.key, f.code_key) is not None))
    if code_equal and not cross:
        if not sem_ok and filed_seen >= NAME_STRONG:
            notes.append(f"header says semester {f.semester}; the code and the name agree, so the semester is ignored")
        elif not sem_ok:
            return Decision("review", None, "semester_conflict",
                            [f"code {f.code} matches but header says semester {f.semester}, subject is semester {filed.semester}"], f)
        if _scheme_conflict(f.scheme, filed):
            for t in catalog.with_code(program, f.code_key):
                if t.id != filed.id and t.scheme == f.scheme and _semester_ok(f.semester, t) and name_seen(text, t.name, words) >= NAME_OK:
                    return Decision("move", t.id, "scheme_sibling", [f"header scheme {f.scheme}; sibling subject has it"], f)
            return Decision("review", None, "scheme_conflict", [f"header scheme {f.scheme}, subject scheme {filed.scheme}"], f)
        if filed_seen < NAME_OK:
            # RGPV reuses a code for different subjects: is a sibling's name the one printed?
            for t in catalog.with_code(program, f.code_key):
                seen = name_seen(text, t.name, words)
                if t.id != filed.id and _semester_ok(f.semester, t) and seen >= NAME_STRONG and seen - filed_seen >= 0.25:
                    return Decision("move", t.id, "code_reused", [f"code {f.code} is shared; the header prints “{t.name}”"], f)
            if title_ok and similarity(f.title, filed.name) < 0.35:
                return Decision("review", None, "code_matches_name_differs", [f"header title “{f.title}” vs subject “{filed.name}”"], f)
            notes.append("name not readable in the header; the code is the evidence")
        return Decision("keep", None, "code_match", notes, f)

    # ---- 2. the header has a code that is not the filed subject's
    if f.code_key:
        cands = [t for t in catalog.with_code(program, f.code_key) if _semester_ok(f.semester, t) and not _scheme_conflict(f.scheme, t)]
        elective_family = catalog.family(program, f.code_key)
        if elective_family and not re.search(r"\d[A-Z]?$", f.code_key[-1:]) and cands:
            names = ", ".join(sorted({t.name for t in elective_family})[:4])
            return Decision("review", None, "elective_family",
                            [f"{f.code} is an elective bucket; the header does not say which option ({names})"], f)
        if cands:
            scored = sorted(((name_seen(text, t.name, words), t) for t in cands), key=lambda x: -x[0])
            best_s, best = scored[0]
            second = scored[1][0] if len(scored) > 1 else 0.0
            if filed_seen >= NAME_STRONG and best_s < NAME_OK and not cross:
                return Decision("keep", None, "code_alias", [f"header code {f.code}; the printed name is the filed subject's"], f)
            if len(cands) == 1 and best_s >= NAME_OK and best_s + 0.05 >= filed_seen:
                return Decision("move", best.id, "code_match_elsewhere", [f"header code {f.code} is subject “{best.name}”"] + notes, f)
            if best_s >= NAME_STRONG and best_s - second >= 0.15:
                return Decision("move", best.id, "code_and_name", [f"header code {f.code} and name match “{best.name}”"] + notes, f)
            return Decision("review", best.id if len(cands) == 1 else None, "several_candidates" if len(cands) > 1 else "code_only",
                            [f"{len(cands)} subject(s) have code {f.code}; names not confirmed"], f)
        if filed_seen >= NAME_STRONG and sem_ok and not cross:
            return Decision("keep", None, "same_name_other_code", [f"header code {f.code} is not on the site; name matches the filed subject"], f)
        if f.complete and title_ok and not cross:
            return Decision("new_subject", None, "no_such_subject",
                            [f"{f.program} semester {f.semester} {f.code} “{f.title}” ({f.scheme or 'scheme not printed'})"], f)
        return Decision("unsorted", None, "code_unknown", [f"code {f.code} is on no subject and the name is not confirmed"], f)

    # ---- 3. no code in the header: the name is all there is
    if filed_seen >= NAME_STRONG and sem_ok and not cross:
        return Decision("keep", None, "name_match", notes, f)
    # only subjects sharing a real word with the header can match: skips ~99% of the catalogue
    pool = [t for t in catalog.by_id.values() if t.program == program and _semester_ok(f.semester, t)
            and any(len(w) >= 5 and w in wset for w in norm(t.name).split())]
    scored = sorted(((name_seen(text, t.name, words), t) for t in pool), key=lambda x: -x[0])[:2]
    if scored and scored[0][0] >= 0.9 and scored[0][1].id != filed.id and filed_seen < NAME_OK             and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.1):
        return Decision("review", scored[0][1].id, "name_only", [f"only the name matches: “{scored[0][1].name}”"], f)
    return Decision("unverified", None, "no_evidence", notes or ["nothing in the header to compare"], f)
