# Paperwala paper-cleaning pipeline

Removes ads, watermarks and contact numbers from exam-paper PDFs without
touching the paper itself, and proves it did so. Step 1 of the plan: the core
library, local files only. Drive, Turso, GitHub Actions and the admin review
pages come next and wrap around `process_paper()`.

## Setup

```
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python scripts\fetch_tessdata.py      # pinned hin/eng/osd models, hash-checked
.venv\Scripts\python -m pytest tests               # 33 tests, ~1 min (needs tesseract)
```

Needs the `tesseract` binary (5.x) on PATH, at the Windows default
location, or in `TESSERACT_CMD`. Models are read from `tessdata/` here, not
the system's, so every machine reads pages with the same Hindi model.

## Use

```
python -m pwpipe process IN.pdf OUT.pdf --report r.json --crops crops/
python -m pwpipe discover reports/*.json --min-files 20 --out candidates.csv
python -m pwpipe approve candidates.csv          # rows with approve=y join the blocklist
```

`process` exits 0 when the output verified, 2 when it did not.

## How one paper is cleaned

1. **Analyze** (`pdf.py`). A *scan page* is one upright image covering the
   page and nothing else drawn. Anything unusual takes the redaction path.
2. **OCR** (`ocr.py`). Tesseract, Hindi + English, on the bare scan image (not
   the rendered page, so hidden overlays never confuse it).
3. **Plan** (`rules.py`, `detect.py`). Each line is `ad`, `suspect`, `weak` or
   nothing. Adjacent ad/suspect lines containing at least one `ad` line form an
   ad block; `weak` lines sandwiched inside it join. A block whose box touches
   any other real word, or that contains a line longer than any real ad,
   is a **conflict**: not erased, sent to review.
4. **Clean** (`clean.py`). Scan pages are rebuilt with only the scan (dropping
   the A-PDF logo, white boxes, metadata), ad boxes painted white, 1-bit scans
   kept 1-bit. Unchanged JPEGs pass through byte-for-byte. Other pages get
   redactions. Written to `.part` and renamed, so no half-written output.
5. **Verify** (`verify.py`).
   - Pixels: outside the erased boxes the output must equal the source
     (exactly, for lossless scans); inside, it must be white.
   - OCR: no ad line may remain; word retention ≥ 85% as a backstop.
   - Page count, page sizes, file-size ratio.
6. **Crops** (`crops.py`). Before/after image per block for the review page:
   red outline = erased, orange = blocked by the safety rule.

`auto_ok` is true only when nothing needs review **and** verification
passed. Review reasons: `suspect`, `conflict`, `drop_page`, `low_text`,
`rotated_page`, `all_pages_ad`, `verify_failed`.

## State database (`db.py`, `state.py`)

Every paper's progress lives in the Turso database `paperwala-pipeline`
(Mumbai), reached over Turso's HTTP API with urllib - no libsql driver, so it
works on Windows and Linux alike. Tests use a local SQLite file with the same
SQL, and also run against the separate `paperwala-pipeline-test` database
when `PIPELINE_TEST_DB_URL` is set (CI sets it).

```
python -m pwpipe migrate
python -m pwpipe seed ../mpboardonline.com-catalog/mpboardonline.com-catalog --catalog mpboardonline.com
python -m pwpipe status
```

A paper's `stage` is the last step it completed
(`discovered → downloaded → processed → staged → approved → published →
live_verified`); `status` is `ready`, `leased`, `review`, `dead` or `done`.

- **Claim** is one `UPDATE … RETURNING`, so two jobs never get the same paper.
- **Leases expire** (30 min, renewable). A job that dies stops renewing and
  its papers are reclaimed - and that counts as an attempt, so a PDF that
  crashes the worker ends `dead` after 5 tries instead of looping.
- **Writes are fenced** on stage + lease owner: a job whose lease was taken
  over cannot overwrite the newer result.
- **Retries are safe**: a repeated `advance` whose first try committed is
  recognized and reported as success.
- **Failures back off** 1 min → 5 min → 30 min → 2 h, then `dead`.
- **Every transition** is written to `events` in the same transaction.
- **Re-seeding** refreshes catalog fields only; it never resets progress.

Seeded 2026-10-28: 37,902 papers from 13 sources (7.1 GB, 77 byte-identical
duplicates). `RGPV-BTech-Catalog` has no Drive ids in any of its 3,130 rows
and contributes nothing.

Credentials live in WSL `~/.turso-env-pipeline` (mode 600): the pipeline URL,
a token for GitHub Actions, a separate token for Render (revocable on its
own), and the test database's URL and token. The repo secrets
`PIPELINE_DB_URL/TOKEN` and `PIPELINE_TEST_DB_URL/TOKEN` hold the same values.

## The blocklist

`blocklists/seed.json` is data, versioned by content hash; every report records
the version it was cleaned with. Domains match only with a `www`/TLD attached
(so "ASSAM BOARD OF…" in a header is not `assamboard.com`). Official
`.nic.in/.gov.in/.ac.in/.edu.in` sites are never flagged. Phones count only as
ten digits starting 6-9, so codes, roll numbers and years never match.

## Lessons already paid for

- Tesseract's `tsv` *config name* is looked up inside the tessdata dir; with a
  pinned dir it silently falls back to plain text. Use `-c tessedit_create_tsv=1`.
  Non-TSV output is now an error, never "zero words".
- `insert_image()` de-duplicates identical images, so swapping the stream of a
  shared placeholder put one page's scan on every page. Images are now written
  as their own objects and placed by xref.
- `update_stream()` deflates what it is given; pre-zlibbed data got
  compressed twice.
- OCR word counts are not proof: erasing a block changes Tesseract's layout,
  so a few words elsewhere re-read differently (7 of 121). The pixel check is
  the proof; retention is the backstop.
