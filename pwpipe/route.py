"""Routing: check each paper's own header against where the site files it.

For every cleaned paper, from the page-1 OCR its cleaning report already holds:

1. **Rules, no AI.** Year, exam type and paper code by pattern; the subject
   is confirmed when its name, code or an alias appears in the header.
2. **AI only when the rules cannot settle it.** Groq reads the header text;
   if Groq is unavailable or unsure, Gemini reads the same text (and the
   header image when one is given). Both must answer in a fixed JSON shape,
   and every answer is checked against the site's own subject list - a model
   can never introduce a subject the site does not have.
3. **Act within limits.**
   - Empty site fields (exam type, year) are filled when the evidence is
     clear. A value already on the site is never overwritten.
   - A paper that looks filed under the wrong subject becomes a "move"
     proposal in the site's review queue; it moves only when a person
     approves it there (the sync job applies it).

Out of AI quota is never a failure: the paper waits for the next run.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .keys import KeyPool, QuotaExhausted

HEADER_SHARE = 0.32                  # top of page 1 holds the paper's identity
# The "-latest" alias follows Google's current Flash model: a pinned name
# (gemini-2.5-flash) was withdrawn for new keys and answered 404.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL_ROUTE", "gemini-flash-latest")
GROQ_MODEL = os.environ.get("GROQ_MODEL_ROUTE", "openai/gpt-oss-120b")
GEMINI_PER_DAY = int(os.environ.get("GEMINI_PER_DAY", "200"))
GROQ_PER_DAY = int(os.environ.get("GROQ_PER_DAY", "900"))
MOVE_MIN_SCORE = 0.86                # name similarity a proposed subject must reach

EXAM_TYPES = [
    # (canonical, patterns) - the canonical names match the site's own words.
    ("Quarterly", [r"quarterly", r"त्रैमासिक", r"trimasik"]),
    # अर्धवार्षिक, also printed अर्द्धवार्षिक.
    ("Half Yearly", [r"half[\s-]*yearly", r"अर्(?:द्)?धवार्षिक", r"ardhvarshik"]),
    ("Pre-board", [r"pre[\s-]*board", r"प्री[\s-]*बोर्ड"]),
    ("Supplementary", [r"supplementary", r"पूरक"]),
    # Not the tail of अर्धवार्षिक परीक्षा (half yearly).
    ("Annual", [r"\bannual\b", r"(?<!ध)वार्षिक परीक्षा"]),
    ("End Semester", [r"end[\s-]*sem", r"\bsemester examination\b"]),
    ("Mid Semester", [r"mid[\s-]*sem"]),
    ("Model Paper", [r"model\s*paper", r"मॉडल\s*पेपर"]),
]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_YEAR = re.compile(r"(?<!\d)(19[89]\d|20[0-3]\d)(?:\s*[-/–]\s*(\d{2,4}))?(?!\d)")
_CODE = re.compile(r"\b([A-Z]{2,5})\s*[-–]?\s*(\d{3,4}[A-Z]?)\b")
_NORM = re.compile(r"[^0-9a-zऀ-ॿ]+")


def norm(text: str) -> str:
    return _NORM.sub(" ", (text or "").lower()).strip()


# --- 1. rules -------------------------------------------------------------------

def header_text(report: dict) -> str:
    pages = report.get("pages") or []
    if not pages:
        return ""
    page = pages[0]
    height = (page.get("raster_size") or [0, 0])[1] or 1
    lines = [l for l in page.get("lines", [])
             if l.get("bbox") and l["bbox"][1] < height * HEADER_SHARE and l.get("kind") != "ad"]
    return "\n".join(l["text"] for l in lines)


@dataclass
class Facts:
    year: int | None = None
    month: str = ""
    exam_type: str = ""
    codes: list[str] = field(default_factory=list)


def rule_facts(text: str) -> Facts:
    low = text.lower()
    f = Facts()
    years = []
    for m in _YEAR.finditer(text):
        start = int(m.group(1))
        end = m.group(2)
        # A session "2026-27" is the exam of the second year.
        if end and len(end) == 2 and int(end) == (start + 1) % 100:
            years.append(start + 1)
        else:
            years.append(start)
    if years:
        f.year = max(set(years), key=years.count)
    for canon, pats in EXAM_TYPES:
        if any(re.search(p, low) for p in pats):
            f.exam_type = canon
            break
    for i, m in enumerate(MONTHS):
        if re.search(rf"\b{m}[a-z]*\b", low):
            f.month = m.title()
            break
    f.codes = sorted({f"{a}-{b}" for a, b in _CODE.findall(text)})
    return f


def subject_seen(text: str, subject: dict) -> bool:
    """The filed subject's name, code or an alias is printed in the header."""
    hay = norm(text)
    names = [subject.get("name", "")] + [a for a in (subject.get("aliases") or "").split(",")]
    for n in names:
        n = norm(n)
        if len(n) >= 4 and n in hay:
            return True
    code = norm(subject.get("code", "")).replace(" ", "")
    return bool(code) and len(code) >= 4 and code in hay.replace(" ", "")


# --- 2. AI ------------------------------------------------------------------------

PROMPT = """You read the header of an Indian exam question paper (OCR text, Hindi or English, may be noisy).
Answer ONLY with JSON: {"subject": str, "paper_code": str, "year": int|null, "month": str, "exam_type": str, "confident": bool}
- subject: the subject name as printed, in English if both are printed. "" if not printed.
- exam_type: one of Quarterly, Half Yearly, Annual, Pre-board, Supplementary, End Semester, Mid Semester, Model Paper, or "".
- confident: false if the text is too garbled to be sure.
Header:
"""


def _post_json(url: str, body: dict, headers: dict, timeout: int = 60) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 # Groq's Cloudflare front refuses urllib's default
                                 # User-Agent with "403 error code: 1010".
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "paperwala-pipeline/1.0", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        retry = exc.headers.get("retry-after")
        return exc.code, {"retry_after": float(retry) if retry and retry.isdigit() else None}
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, {}


def _parse(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class AI:
    def __init__(self, groq: KeyPool | None, gemini: KeyPool | None, post=_post_json):
        self.groq, self.gemini, self.post = groq, gemini, post

    @classmethod
    def from_env(cls, shared=None) -> "AI":
        return cls(KeyPool.from_env("groq", GROQ_PER_DAY, shared=shared),
                   KeyPool.from_env("gemini", GEMINI_PER_DAY, shared=shared))

    def _groq(self, header: str) -> dict | None:
        key = self.groq.acquire()
        status, data = self.post(
            "https://api.groq.com/openai/v1/chat/completions",
            {"model": GROQ_MODEL, "temperature": 0, "response_format": {"type": "json_object"},
             "messages": [{"role": "user", "content": PROMPT + header[:3000]}]},
            {"Authorization": f"Bearer {key}"})
        self.groq.report(key, status, data.get("retry_after"))
        if status != 200:
            return None
        return _parse(data.get("choices", [{}])[0].get("message", {}).get("content", ""))

    def _gemini(self, header: str, image_png: bytes | None) -> dict | None:
        key = self.gemini.acquire()
        parts = [{"text": PROMPT + header[:3000]}]
        if image_png:
            parts.append({"inline_data": {"mime_type": "image/png",
                                          "data": base64.b64encode(image_png).decode()}})
        status, data = self.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            {"contents": [{"parts": parts}],
             "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}},
            {"x-goog-api-key": key})
        self.gemini.report(key, status, data.get("retry_after"))
        if status != 200:
            return None
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            return None
        return _parse(text)

    def read(self, header: str, image_png: bytes | None = None) -> dict | None:
        """Groq first (text is enough for most headers), Gemini when Groq has
        no answer or is unsure. QuotaExhausted only if both are out."""
        answer, out_of_quota = None, 0
        if self.groq and len(self.groq):
            try:
                answer = self._groq(header)
            except QuotaExhausted:
                out_of_quota += 1
        if (answer is None or not answer.get("confident")) and self.gemini and len(self.gemini):
            try:
                second = self._gemini(header, image_png)
                answer = second or answer
            except QuotaExhausted:
                out_of_quota += 1
        if answer is None and out_of_quota:
            raise QuotaExhausted("no AI quota left for routing")
        return answer


# --- 3. decide ----------------------------------------------------------------------

@dataclass
class Plan:
    fills: dict = field(default_factory=dict)        # column -> value, only for empty columns
    move_to: dict | None = None                      # candidate subject row
    confirmed: bool = False
    notes: list[str] = field(default_factory=list)
    used_ai: bool = False


def best_subject(name: str, code: str, candidates: list[dict]) -> tuple[dict | None, float]:
    best, score = None, 0.0
    code_n = norm(code).replace(" ", "")
    for c in candidates:
        if code_n and code_n == norm(c.get("code", "")).replace(" ", ""):
            return c, 1.0
        for n in [c.get("name", "")] + (c.get("aliases") or "").split(","):
            s = SequenceMatcher(None, norm(name), norm(n)).ratio() if n.strip() else 0
            if s > score:
                best, score = c, s
    return best, score


def plan_paper(header: str, paper: dict, subject: dict, candidates: list[dict],
               ai: AI | None) -> Plan:
    plan = Plan()
    facts = rule_facts(header)
    plan.confirmed = subject_seen(header, subject)
    answer = None
    need_ai = not plan.confirmed or (not facts.exam_type and not paper.get("exam_type"))
    if need_ai and ai is not None and header.strip():
        answer = ai.read(header)
        plan.used_ai = answer is not None

    exam_type = facts.exam_type or ((answer or {}).get("exam_type") or "")
    if exam_type and not paper.get("exam_type") and any(exam_type == c for c, _ in EXAM_TYPES):
        plan.fills["exam_type"] = exam_type
    year = facts.year or (answer or {}).get("year")
    if isinstance(year, int) and not paper.get("year") and 1980 <= year <= 2035:
        plan.fills["year"] = year

    if not plan.confirmed and answer and answer.get("confident") and answer.get("subject"):
        # The filed subject is wrong only if the header names a DIFFERENT
        # subject the site has - never one the model made up.
        filed_score = SequenceMatcher(None, norm(answer["subject"]), norm(subject["name"])).ratio()
        cand, score = best_subject(answer["subject"], answer.get("paper_code", ""),
                                   [c for c in candidates if c["id"] != subject["id"]])
        if cand and score >= MOVE_MIN_SCORE and score > filed_score + 0.15:
            plan.move_to = cand
            plan.notes.append(f"Header reads “{answer['subject']}”"
                              + (f" ({answer['paper_code']})" if answer.get("paper_code") else "")
                              + f"; filed under “{subject['name']}”. Proposed: “{cand['name']}”.")
        elif filed_score >= MOVE_MIN_SCORE:
            plan.confirmed = True
    return plan
