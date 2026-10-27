"""Find ads by repetition across the whole corpus.

Exam papers do not share images or sentences with hundreds of other papers;
a source site's stamp does. So anything that recurs in many files is a
candidate. Recurring is not the same as ad - "सभी प्रश्न हल करना अनिवार्य है"
(all questions are compulsory) is on hundreds of real papers - which is why
candidates go to a human and only approved ones enter the blocklist.
Candidates that already look promotional are ranked first.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from .blocklist import Blocklist
from .rules import AD, SUSPECT, WEAK, classify_line, line_key

MIN_KEY_LEN = 10


def fingerprint(report: dict) -> dict:
    """What one paper contributes to discovery: its image hashes and lines."""
    lines = set()
    for page in report["pages"]:
        for line in page["lines"]:
            key = line_key(line["text"])
            if len(key) >= MIN_KEY_LEN and line["conf"] >= 40:
                lines.add(key)
    return {"paper": report["paper"], "images": sorted(set(report["image_sha1"])),
            "lines": sorted(lines)}


def aggregate(fingerprints: list[dict], bl: Blocklist, min_files: int) -> list[dict]:
    image_files: dict[str, set] = defaultdict(set)
    line_files: dict[str, set] = defaultdict(set)
    for fp in fingerprints:
        for h in fp["images"]:
            image_files[h].add(fp["paper"])
        for key in fp["lines"]:
            line_files[key].add(fp["paper"])

    rank = {AD: 0, SUSPECT: 1, WEAK: 2, None: 3}
    out = []
    for h, files in image_files.items():
        if len(files) >= min_files and h not in bl.image_sha1:
            out.append({"type": "image", "value": h, "files": len(files),
                        "signal": "", "sample": sorted(files)[:5]})
    for key, files in line_files.items():
        if len(files) >= min_files and key not in bl.lines:
            kind, reason = classify_line(key, bl)
            out.append({"type": "line", "value": key, "files": len(files),
                        "signal": kind or "", "reason": reason, "sample": sorted(files)[:5]})
    out.sort(key=lambda c: (rank.get(c["signal"] or None, 3), -c["files"]))
    return out


def write_candidates(candidates: list[dict], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["type", "value", "files", "signal", "reason", "sample", "approve"])
        for c in candidates:
            w.writerow([c["type"], c["value"], c["files"], c["signal"], c.get("reason", ""),
                        " ".join(c["sample"]), ""])


def apply_approved(bl: Blocklist, csv_path: Path) -> Blocklist:
    """Fold rows marked approve=y back into a new blocklist."""
    with open(csv_path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if (row.get("approve") or "").strip().lower() not in ("y", "yes", "1"):
                continue
            target = bl.image_sha1 if row["type"] == "image" else bl.lines
            if row["value"] not in target:
                target.append(row["value"])
    bl.lines.sort()
    bl.image_sha1.sort()
    return bl
