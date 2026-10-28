"""Classify one OCR line: ad, suspect, weak, or nothing.

- ``ad``: erased without a human: a blocklisted domain; any other written-out
  domain that is not official or allowlisted (only its URL words go); a phone
  number next to a contact keyword; a phone number standing alone or with only
  a label ("Mob. 9300930012"); an approved line.
- ``suspect``: promotional-looking but unproven (a phone number inside a
  sentence - "Write 9876543210 in words" is a maths question - or a keyword).
  Joins an ad block it touches; on its own it only sends the paper to review.
- ``weak``: vocabulary that is promotional only inside an ad block.

Paper codes, roll numbers, marks and years never match: phones must be ten
digits starting 6-9, and nothing else here looks at numbers.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from .blocklist import Blocklist

AD, SUSPECT, WEAK = "ad", "suspect", "weak"

# OCR reads digits inside a phone number as look-alike letters
# ("93009300I2"). Only applied to tokens that are mostly digits already.
_DIGIT_LOOKALIKES = str.maketrans({
    "O": "0", "o": "0", "D": "0", "Q": "0", "I": "1", "l": "1", "|": "1", "!": "1",
    "i": "1", "S": "5", "s": "5", "Z": "2", "z": "2", "B": "8", "g": "9", "q": "9",
})
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?91)?[6-9]\d{9}(?!\d)")
_DOMAIN_RE = re.compile(
    r"(?:https?://)?((?:[a-z0-9-]*[a-z][a-z0-9-]*\.)+"
    r"(?:com|in|net|org|info|online|co|xyz|site|io|me|app|live|tech|edu|gov|nic|ac|res))\b"
)
_NON_KEY = re.compile(r"[^0-9a-zऀ-ॿ]+")
LINE_MATCH_RATIO = 0.85


def fix_digits(text: str) -> str:
    out = []
    for token in text.split():
        alnum = [c for c in token if c.isalnum()]
        digits = sum(c.isdigit() for c in alnum)
        if digits >= 3 and digits >= 0.6 * len(alnum):
            token = token.translate(_DIGIT_LOOKALIKES)
        out.append(token)
    return " ".join(out)


def line_key(text: str) -> str:
    """Normalized form used to compare lines across papers and OCR runs."""
    return _NON_KEY.sub(" ", fix_digits(text).lower()).strip()


def find_phones(text: str) -> list[str]:
    fixed = fix_digits(text)
    # Phones are printed as "93009 30012" or "9300-930012".
    collapsed = re.sub(r"(?<=\d)[\s\-.]+(?=\d)", "", fixed)
    return _PHONE_RE.findall(collapsed)


def find_domains(text: str, explicit_only: bool = False) -> list[str]:
    """Domains in a line. OCR splits URLs ("www. pyqonline .com"), so dots
    are rejoined across spaces - but that also turns prose into domains
    ("etc. in the" -> etc.in, "M.P. in" -> m.p.in; 12 false suspects in the
    second pilot). ``explicit_only`` keeps a domain only when it is written
    as one: after www/http, or with no space around its dots."""
    lowered = text.lower()
    joined = re.sub(r"\s*\.\s*(?=[a-z])", ".", lowered)
    out = []
    for m in _DOMAIN_RE.finditer(joined):
        domain = m.group(1).removeprefix("www.")
        if explicit_only and not (m.group(0).startswith(("http", "www")) or
                                  m.group(1).startswith("www.") or domain in lowered):
            continue
        out.append(domain)
    return out


def _domain_label(domain: str) -> str:
    return domain.removeprefix("www.").split(".")[0]


def domain_allowed(domain: str, bl: Blocklist) -> bool:
    if any(domain == s.lstrip(".") or domain.endswith(s) for s in bl.allow_domain_suffixes):
        return True
    return any(domain == a or domain.endswith("." + a) for a in bl.allow_domains)


def _plausible_domain(domain: str, text: str) -> bool:
    """An unlisted domain is erased only if it really looks like one.

    Prose produces fake ones (round 4 of the pilot): "independence.In the"
    (a missing space after a full stop) and OCR noise like "e.com".
    With www/http in front it is a URL. Otherwise the name before the dot
    must have 3+ characters, and an ending written like a sentence start
    (".In", ".Com") does not count; all-caps ("A2ZSUBJCTS.COM") does.
    """
    lowered = text.lower()
    label = domain.split(".")[0]
    if re.search(r"(?:https?:/*|www\.?)\s*" + re.escape(label), lowered):
        return True
    if len(label) < 3:
        return False
    m = re.search(re.escape(domain).replace(r"\.", r"\s*\.\s*"), text, re.IGNORECASE)
    if m:
        ending = re.split(r"\s*\.\s*", m.group(0))[-1]
        if ending[:1].isupper() and not ending.isupper():
            return False
    return True


# Labels printed next to a contact number; a line that is only these plus a
# number is a contact line, not a question.
_PHONE_LABELS = re.compile(
    r"\b(?:mob(?:ile)?|ph(?:one)?|tel|cell|call|contact|no|number|whats\s*app|helpline)\b"
    r"|[+:.\-/()@]")


def _bare_phone(text: str) -> bool:
    rest = re.sub(r"[\d\s]", "", _PHONE_LABELS.sub(" ", fix_digits(text).lower()))
    return sum(c.isalnum() for c in rest) <= 2


def classify_line(text: str, bl: Blocklist) -> tuple[str | None, str]:
    lowered = text.lower()
    compact = re.sub(r"[^a-z0-9]", "", lowered)

    # Dots rejoined across OCR spaces ("pyqonline .com"), then letters only,
    # keeping the dots: "www.pyqonline.com" -> "www.pyqonline.com".
    dotted = re.sub(r"[^a-z0-9.]", "", re.sub(r"\s*\.\s*", ".", lowered))
    for domain in bl.domains:
        label = _domain_label(domain)
        # The label must carry "www" or a dot + TLD. Not just a TLD after it:
        # "AKU Bihar in 2019" squeezes to "akubiharin" and "Assam Board in"
        # to "assamboardin" - board names are printed on their own papers.
        if len(label) >= 6 and (f"www{label}" in compact or re.search(
                rf"{label}\.(?:com|corn|con|cam|co|in|net)", dotted)):
            return AD, f"domain:{domain}"

    key = line_key(text)
    if key:
        for approved in bl.lines:
            if 0.7 <= len(key) / max(len(approved), 1) <= 1.3 and \
                    SequenceMatcher(None, key, approved).ratio() >= LINE_MATCH_RATIO:
                return AD, f"line:{approved[:40]}"

    keyword = next((k for k in bl.keywords if k in lowered), None)
    phones = find_phones(text)
    if phones and keyword:
        return AD, f"phone+keyword:{keyword}"

    unknown = [d for d in find_domains(text, explicit_only=True)
               if not domain_allowed(d, bl) and _plausible_domain(d, text)]
    if unknown:
        # Promotion for some other website: erased (URL words only).
        return AD, f"domain:{unknown[0]}"
    if phones:
        if _bare_phone(text):
            return AD, "phone:bare"
        return SUSPECT, "phone"
    if keyword:
        return SUSPECT, f"keyword:{keyword}"
    if any(w in lowered for w in bl.weak_words):
        return WEAK, "weak"
    return None, ""
