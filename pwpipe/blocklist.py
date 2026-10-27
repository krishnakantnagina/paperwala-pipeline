"""The approved list of what counts as an ad.

It is data, not code, so the review page can extend it without a deploy, and
its version (a content hash) is stamped on every paper cleaned with it - a new
version re-queues exactly the papers it could change.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

SEED_PATH = Path(__file__).resolve().parent.parent / "blocklists" / "seed.json"


@dataclass
class Blocklist:
    # Ad domains. Matched loosely: the label before the TLD ("pyqonline") is
    # searched in the line with punctuation removed, since OCR splits and
    # misreads URLs ("www. pyqonline .corn").
    domains: list[str] = field(default_factory=list)
    # Domains that are never ads: official board and university sites printed
    # on real papers. Matched as suffixes.
    allow_domain_suffixes: list[str] = field(default_factory=list)
    # Phrases that mark a line as promotional. Alone they only make a line
    # suspect; next to a confirmed ad line they join the ad block.
    keywords: list[str] = field(default_factory=list)
    # Words that only count when sandwiched inside an ad block (the Hindi
    # "send your old paper" line has no URL or keyword of its own).
    weak_words: list[str] = field(default_factory=list)
    # Whole ad lines approved from corpus discovery, stored normalized.
    lines: list[str] = field(default_factory=list)
    # sha1 of raw image streams that are logos/watermarks.
    image_sha1: list[str] = field(default_factory=list)

    @property
    def version(self) -> str:
        canon = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]

    @classmethod
    def load(cls, path: str | Path = SEED_PATH) -> "Blocklist":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        known = set(cls.__dataclass_fields__)
        unknown = set(data) - known
        if unknown:
            # A typo'd key would silently disable a whole rule.
            raise ValueError(f"unknown blocklist keys: {sorted(unknown)}")
        return cls(**data)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")
