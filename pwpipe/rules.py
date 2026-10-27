"""Classify one OCR line: ad, suspect, weak, or nothing.

- ``ad``: certain enough to erase without a human (an approved domain or line,
  or a phone number next to a contact keyword).
- ``suspect``: promotional-looking but unproven (an unknown domain, a bare
  phone number, a keyword). Joins an ad block it touches; on its own it only
  sends the paper to review.
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


def find_domains(text: str) -> list[str]:
    lowered = re.sub(r"\s*\.\s*(?=[a-z])", ".", text.lower())
    return [m.group(1).removeprefix("www.") for m in _DOMAIN_RE.finditer(lowered)]


def _domain_label(domain: str) -> str:
    return domain.removeprefix("www.").split(".")[0]


def classify_line(text: str, bl: Blocklist) -> tuple[str | None, str]:
    lowered = text.lower()
    compact = re.sub(r"[^a-z0-9]", "", lowered)

    for domain in bl.domains:
        label = _domain_label(domain)
        # The label must carry a "www" or TLD with it: "assamboard" alone is
        # also the start of "Assam Board of Secondary Education" in a header.
        if len(label) >= 6 and re.search(
                rf"www{label}|{label}(?:com|corn|con|cam|co|in|net)", compact):
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

    unknown = [d for d in find_domains(text)
               if not any(d == s.lstrip(".") or d.endswith(s) for s in bl.allow_domain_suffixes)]
    if unknown:
        return SUSPECT, f"domain?:{unknown[0]}"
    if phones:
        return SUSPECT, "phone"
    if keyword:
        return SUSPECT, f"keyword:{keyword}"
    if any(w in lowered for w in bl.weak_words):
        return WEAK, "weak"
    return None, ""
