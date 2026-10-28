"""Tesseract OCR through its command-line binary.

The CLI rather than pytesseract: one dependency fewer, and its TSV output
already carries every word's box and confidence, which is all detection needs.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LANGS = "hin+eng"
_WINDOWS_DEFAULT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")


class OcrError(RuntimeError):
    pass


@dataclass(frozen=True)
class Word:
    text: str
    conf: float
    block: int
    par: int
    line: int
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass
class Line:
    words: list[Word] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return (
            min(w.x0 for w in self.words),
            min(w.y0 for w in self.words),
            max(w.x1 for w in self.words),
            max(w.y1 for w in self.words),
        )

    @property
    def height(self) -> int:
        b = self.bbox
        return b[3] - b[1]

    @property
    def conf(self) -> float:
        return sum(w.conf for w in self.words) / len(self.words)

    def to_dict(self) -> dict:
        return {"text": self.text, "bbox": list(self.bbox), "conf": round(self.conf, 1)}


def tesseract_cmd() -> str:
    explicit = os.environ.get("TESSERACT_CMD")
    if explicit:
        return explicit
    found = shutil.which("tesseract")
    if found:
        return found
    if _WINDOWS_DEFAULT.exists():
        return str(_WINDOWS_DEFAULT)
    raise OcrError("tesseract not found; install it or set TESSERACT_CMD")


def tessdata_dir() -> Path | None:
    """The pinned models in pipeline/tessdata win over the system ones, so a
    laptop and a CI runner read pages with the same Hindi model."""
    explicit = os.environ.get("TESSDATA_DIR")
    if explicit:
        return Path(explicit)
    local = PIPELINE_ROOT / "tessdata"
    if (local / "hin.traineddata").exists():
        return local
    return None


# Tesseract's thresholding_method: 0 = global Otsu (its default), 2 = Sauvola,
# which thresholds each neighbourhood on its own. Global Otsu fails on a grey
# scan inside white margins (BU papers): the threshold lands between grey and
# white, the whole grey area turns black, and only the URL printed on white is
# read - 2 words of a 250-word page.
OTSU, SAUVOLA = 0, 2


def ocr_image(img: Image.Image, langs: str = DEFAULT_LANGS, psm: int = 3,
              timeout: int = 600, threshold: int = OTSU) -> list[Word]:
    if img.mode not in ("L", "RGB"):
        img = img.convert("L")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    cmd = [tesseract_cmd(), "stdin", "stdout", "-l", langs, "--psm", str(psm)]
    tdir = tessdata_dir()
    if tdir is not None:
        cmd += ["--tessdata-dir", str(tdir)]
    # Not the "tsv" config name: configs are looked up inside the tessdata
    # dir, the pinned one has none, and tesseract then falls back to plain
    # text with only a warning.
    cmd += ["-c", "tessedit_create_tsv=1"]
    if threshold != OTSU:
        cmd += ["-c", f"thresholding_method={threshold}"]
    try:
        proc = subprocess.run(cmd, input=buf.getvalue(), capture_output=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise OcrError(f"tesseract timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise OcrError(proc.stderr.decode("utf-8", "replace")[-500:])
    return parse_tsv(proc.stdout.decode("utf-8", "replace"))


def parse_tsv(tsv: str) -> list[Word]:
    # Anything but TSV means tesseract misread its options. Reporting that as
    # "no words on the page" would let a page through unchecked.
    if not tsv.startswith("level\t"):
        raise OcrError(f"tesseract did not return TSV: {tsv[:80]!r}")
    words: list[Word] = []
    # QUOTE_NONE: OCR text can contain a stray quote, which must not swallow
    # the rest of the file as one field.
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)
    for row in reader:
        if row.get("level") != "5":
            continue
        text = (row.get("text") or "").strip()
        conf = float(row.get("conf") or -1)
        if not text or conf < 0:
            continue
        left, top = int(row["left"]), int(row["top"])
        words.append(Word(
            text=text, conf=conf, block=int(row["block_num"]), par=int(row["par_num"]),
            line=int(row["line_num"]), x0=left, y0=top,
            x1=left + int(row["width"]), y1=top + int(row["height"]),
        ))
    return words


def group_lines(words: list[Word]) -> list[Line]:
    lines: dict[tuple[int, int, int], Line] = {}
    for w in words:
        lines.setdefault((w.block, w.par, w.line), Line()).words.append(w)
    return list(lines.values())
