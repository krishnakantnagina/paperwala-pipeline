"""Download the pinned Tesseract models into pipeline/tessdata, hash-checked.

Pinned rather than taken from the OS package: the planner's thresholds were
tuned against these exact models, and a different Hindi model reads the same
scan differently.

    python scripts/fetch_tessdata.py
"""

import hashlib
import sys
import urllib.request
from pathlib import Path

BASE = "https://github.com/tesseract-ocr/tessdata/raw/4.1.0/"
MODELS = {
    "hin.traineddata": "cc76d09fa4fed1c7a4674046e25e63760d0c9bfdce390a52113462c34a556ee6",
    "eng.traineddata": "daa0c97d651c19fba3b25e81317cd697e9908c8208090c94c3905381c23fc047",
    "osd.traineddata": "e19f2ae860792fdf372cf48d8ce70ae5da3c4052962fe22e9de1f680c374bb0e",
}
DEST = Path(__file__).resolve().parent.parent / "tessdata"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    DEST.mkdir(exist_ok=True)
    for name, digest in MODELS.items():
        path = DEST / name
        if path.exists() and sha256(path) == digest:
            print(f"ok      {name}")
            continue
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(BASE + name, tmp)
        if sha256(tmp) != digest:
            tmp.unlink()
            print(f"FAILED  {name}: checksum mismatch", file=sys.stderr)
            return 1
        tmp.replace(path)
        print(f"fetched {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
