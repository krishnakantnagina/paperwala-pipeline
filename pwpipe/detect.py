"""Turn one page's OCR lines into a plan: which boxes to whiten.

An ad *block* is a run of vertically adjacent ad/suspect lines that contains
at least one confirmed ad line, plus any weak line sitting inside its span.
That is how the pyqonline footer is caught whole: its URL line is an ad, its
WhatsApp line is phone+keyword, and the Hindi "send your old paper" line in
the middle has no signal of its own but is sandwiched.

The safety rule is absolute: a block is only applied if its box touches no
word that is not part of the block. Otherwise it is recorded as a conflict
and the paper goes to a human. Erasing a question is worse than leaving an ad.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from statistics import median

from .blocklist import Blocklist
from .ocr import Line
from .pdf import PageInfo
from .rules import AD, SUSPECT, WEAK, classify_line

LINE_GAP_FACTOR = 2.0       # max vertical gap between block lines, in line heights
REAL_WORD_CONF = 50         # a word this confident is content unless proven ad
DROP_MAX_REAL_WORDS = 3     # a page with ads and at most this much else is an ad page
LOW_TEXT_WORDS = 10         # fewer confident words than this: OCR cannot vouch for the page
# Real ad lines are short ("Send your old paper & get 10₹ to 40₹" is 27
# letters). A longer line that matched is probably a question that happens to
# contain a URL or a keyword, and erasing works per line, not per word.
MAX_AD_LINE_ALNUM = 70


@dataclass
class OcrLine:
    text: str
    bbox: tuple[int, int, int, int]
    conf: float
    kind: str | None
    reason: str
    words: list[tuple[str, float, tuple[int, int, int, int]]]
    region: int | None = None      # index into PagePlan.regions when part of an ad block

    @property
    def real_words(self) -> list[tuple[int, int, int, int]]:
        return [b for t, c, b in self.words if c >= REAL_WORD_CONF and _alnum(t) >= 2]


@dataclass
class Region:
    bbox: tuple[int, int, int, int]
    lines: list[str]
    reasons: list[str]
    conflicts: list[str] = field(default_factory=list)

    @property
    def safe(self) -> bool:
        return not self.conflicts


@dataclass
class PagePlan:
    number: int
    mode: str                      # "rebuild" (scan page) or "redact"
    raster_size: tuple[int, int]
    regions: list[Region] = field(default_factory=list)
    suspects: list[dict] = field(default_factory=list)
    drop: bool = False
    review: list[str] = field(default_factory=list)
    # sha1, not xref: xrefs are renumbered when the page is copied out.
    delete_image_sha1: list[str] = field(default_factory=list)
    # PDF point = origin + raster pixel * scale (see pdf.Raster).
    origin: tuple[float, float] = (0.0, 0.0)
    scale: tuple[float, float] = (1.0, 1.0)
    lines: list[OcrLine] = field(default_factory=list)

    @property
    def applied_regions(self) -> list[Region]:
        return [r for r in self.regions if r.safe] if "rotated_page" not in self.review else []

    def applied_lines(self) -> list[OcrLine]:
        """Lines that the cleaner erases (members of a region that is applied)."""
        applied = {id(r) for r in self.applied_regions}
        return [l for l in self.lines
                if l.region is not None and id(self.regions[l.region]) in applied]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["lines"] = [{"text": l.text, "bbox": list(l.bbox), "conf": round(l.conf, 1),
                       "kind": l.kind, "reason": l.reason, "region": l.region}
                      for l in self.lines]
        return d


def _alnum(text: str) -> int:
    return sum(c.isalnum() for c in text)


def _intersects(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _union(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def classify_lines(lines: list[Line], bl: Blocklist) -> list[OcrLine]:
    out = []
    for line in lines:
        kind, reason = classify_line(line.text, bl)
        out.append(OcrLine(line.text, line.bbox, line.conf, kind, reason,
                           [(w.text, w.conf, w.bbox) for w in line.words]))
    return out


def _adjacent(a: OcrLine, b: OcrLine, max_gap: float) -> bool:
    (ax0, ay0, ax1, ay1), (bx0, by0, bx1, by1) = a.bbox, b.bbox
    vgap = max(by0 - ay1, ay0 - by1, 0)
    hgap = max(bx0 - ax1, ax0 - bx1, 0)
    return vgap <= max_gap and hgap <= 3 * max_gap


def _clusters(nodes: list[OcrLine], max_gap: float) -> list[list[OcrLine]]:
    seen: set[int] = set()
    groups = []
    for i in range(len(nodes)):
        if i in seen:
            continue
        stack, group = [i], []
        seen.add(i)
        while stack:
            j = stack.pop()
            group.append(nodes[j])
            for k in range(len(nodes)):
                if k not in seen and _adjacent(nodes[j], nodes[k], max_gap):
                    seen.add(k)
                    stack.append(k)
        groups.append(group)
    return groups


def plan_page(info: PageInfo, raster_size: tuple[int, int], lines: list[OcrLine],
              bl: Blocklist) -> PagePlan:
    plan = PagePlan(number=info.number, mode="rebuild" if info.is_scan else "redact",
                    raster_size=raster_size, lines=lines)
    if not info.is_scan:
        plan.delete_image_sha1 = sorted({i.sha1 for i in info.images if i.sha1 in bl.image_sha1})

    heights = [l.bbox[3] - l.bbox[1] for l in lines if l.conf >= 30] or [20]
    line_h = median(heights)
    max_gap = LINE_GAP_FACTOR * line_h
    pad = max(6, int(0.4 * line_h))
    width, height = raster_size

    flagged = [l for l in lines if l.kind in (AD, SUSPECT)]
    members: set[int] = set()
    for group in _clusters(flagged, max_gap):
        if not any(l.kind == AD for l in group):
            continue
        span = _union([l.bbox for l in group])
        in_group = {id(l) for l in group}
        # Pull in weak lines (and stray flagged ones) lying inside the span.
        for l in lines:
            if id(l) in in_group:
                continue
            cy = (l.bbox[1] + l.bbox[3]) / 2
            inside = span[1] <= cy <= span[3] and _intersects(l.bbox, span)
            if inside and l.kind in (AD, SUSPECT, WEAK):
                group.append(l)
        span = _union([l.bbox for l in group])
        members.update(id(l) for l in group)
        for l in group:
            l.region = len(plan.regions)
        region = Region(bbox=span, lines=[l.text for l in group],
                        reasons=sorted({l.reason for l in group if l.reason}))
        plan.regions.append(region)

    for region in plan.regions:
        outsiders = [l for l in lines if id(l) not in members]
        for p in (pad, 0):
            box = (max(0, region.bbox[0] - p), max(0, region.bbox[1] - p),
                   min(width, region.bbox[2] + p), min(height, region.bbox[3] + p))
            hits = [l.text for l in outsiders
                    if any(_intersects(wb, box) for wb in l.real_words)]
            if not hits:
                region.bbox, region.conflicts = box, []
                break
            region.conflicts = hits
        long_lines = [t for t in region.lines if _alnum(t) > MAX_AD_LINE_ALNUM]
        region.conflicts += [f"long line: {t}" for t in long_lines]

    plan.suspects = [{"text": l.text, "reason": l.reason, "bbox": list(l.bbox)}
                     for l in lines if l.kind == SUSPECT and id(l) not in members]

    if plan.suspects:
        plan.review.append("suspect")
    if any(not r.safe for r in plan.regions):
        plan.review.append("conflict")
    if info.rotation != 0 and not info.is_scan and plan.regions:
        plan.review.append("rotated_page")
    real_words = sum(len(l.real_words) for l in lines)
    if real_words < LOW_TEXT_WORDS and not plan.regions:
        # Blank, unreadable, upside-down or not text at all: nothing here can
        # be verified by OCR, so a person looks at it.
        plan.review.append("low_text")
    if plan.regions and all(r.safe for r in plan.regions):
        real_left = sum(len(l.real_words) for l in lines if id(l) not in members)
        if real_left <= DROP_MAX_REAL_WORDS:
            plan.drop = True
            plan.review.append("drop_page")
    return plan
