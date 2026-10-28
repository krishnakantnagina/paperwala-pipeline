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
  the proof; retention is the backstop - and on lossless pages that already
  passed the pixel check it is only a note.

### From the 50-paper MP Board pilot (2026-10-28, v0.1.0 → v0.2.0)

- **The source sites stamp their URL in the margin of every page**, and OCR
  reads it as one line with the question beside it ("sound wave of frequency
  20 Hz. https://www.mpboardonline.com"). Erasing whole lines took question
  text with it ("bee loud glade.", "नाम लिखिए।"). A URL line now loses only its
  URL words; the rest of the line is protected like any other text.
- **One of those passed verification**, because the pixel check verifies the
  cleaner against the plan. `verify.erased_content` now checks the plan
  itself: every word inside an erased box must be ad material.
- **`save(clean=True)` changed Word-made papers** (unembedded fonts): 0.9 % of
  pixels moved. Removed; garbage + deflate change nothing.
- **A redacted text page can come out looking like a scan**, so the verifier
  read it as an image while the source was rendered. Both are now rendered.
- **Turso rejects unused named SQL parameters** that SQLite ignores; `db._stmt`
  sends only the ones a statement references.

Result on the 10 papers that failed: 9 clean and verified, 1 correctly sent
to review (its URL touches question text).

## Running by itself (all on GitHub Actions)

| Workflow | When | Does |
|---|---|---|
| `work.yml` | every 6 h (16 workers) | download → clean → verify → upload privately; auto-approve or send to review |
| `publish.yml` | every 2 h, while repo variable `PUBLISH_ENABLED` = `true` | **sync** with the site's review queue, then **publish** approved papers (switch the site's link to the cleaned copy, 20 per batch, site checked after each) |
| `route.yml` | every 3 h | **routing**: read each paper's header (rules first, Groq/Gemini only when needed); fill empty exam type / year; propose subject moves for a person |
| `health.yml` | daily | state DB, Drive login and space, stalls, dead papers → opens a "Pipeline health" issue (emails the owner); monthly keep-alive commit |

- **The chain (does not wait for GitHub's scheduler).** On this repo the schedules fired nothing for the first hours after they were added, so each run ends with a `next` job: `pwpipe next` (`chain.py`) asks whether work is left **and** whether this run moved any of it, and `scripts/start_run.sh` then starts the next run unless one of that workflow is already queued or running, so two chains merge instead of doubling. Cleaning chains only for runs started by the schedule or with `chain` ticked, so a small pilot never turns into a full run. Every cleaning run also starts publish and routing. Publish starts its next run at once while approved papers wait; otherwise it waits until two hours after it began, so review decisions still sync every 2 h, and it starts the daily health run. A run that moved nothing, or failed, never restarts at once, so a fault cannot loop. The schedules stay as the backup.
- **Stop publishing:** set `PUBLISH_ENABLED` to `false`. That also ends the publish chain; to restart, set it back to `true` and start publish once by hand. **Undo a paper:** `python -m pwpipe unpublish <original drive id> --reason "..."` (the original file was never touched).
- **The paperwala.org mark** (`stamp.py`) is vector text at the foot of each page, only where the bottom is blank; verify checks it.
- **Review without credentials on the site:** `sync.py` copies papers waiting for a person into the site's `pipeline_review` table (site migration `0009`), reads the decisions made in `/admin/review` back (publish / keep original / clean again; for routing items move / leave), and acts on students' `paper_reports`. Each row is applied once (`synced`).
- **More AI keys:** add `GEMINI_API_KEY_2`…`_5` / `GROQ_API_KEY_2`…`_5` as repo secrets (`gh secret set -f keys.txt`). `keys.py` rotates them, rests a key on 429/5xx, retires it on 401/403, and shares daily counts across workers. Out of quota never fails a paper: it waits.
- **Live database access** (`LIVE_DB_URL`, `LIVE_DB_TOKEN`) is used only by publish, sync and route.

### From the owner's first review decisions (2026-10-28, v0.3.0)

Five MP Board papers sent back with notes; all five now pass on their own, and
10 papers that already passed (every source) come out exactly as before.

- **URL stamped between two Hindi lines** ("में लिखना भूल गए। https://www.mpboardonline.com"):
  the matras of the line above and below reach 2-6 px into the URL's box, and the
  safety rule refused the whole erase. The box is now trimmed off the other lines'
  slivers when at least 60% of the URL's height is still erased. A word *beside*
  the URL on its own line still blocks it.
- **A copy of the URL the first reading missed** (run into a neighbouring word,
  "किन अन्तर बिग MPboardonline.com"): the verifier's reading of the cleaned page
  finds it, so it is added and the paper cleaned again, up to two extra rounds
  (`second_looks` in the report).
- **A footer URL with a printed rule through it** reads as nothing. `ruled.py`
  whitens long horizontal rules in a copy and re-reads a strip around each one
  (sparse-text mode; page mode read garbage).
- **Table rows are not phone numbers**: "70 60 38 32 48" was joined into
  7060383248. Only digit groups of 3+ are joined now.
- **Low word retention on a rendered page that is pixel-identical** outside the
  erased boxes is a note, not a failure: tables make Tesseract re-read the layout
  (retention 0.55 on a page whose only change was three erased URLs).

### From the owner's second round of review decisions (2026-10-28, v0.4.0)

- **Owner's rule: an identified ad is erased even if it clips the question beside
  it.** Before, a box that touched other words was refused and the paper went to
  review ("conflict"). Now the ad's *exact* box (never padded) is erased and the
  paper is not held for it; `Region.forced` / `clipped` record how many words were
  touched, and the verifier does not count those words as lost.
- **A site name OCR split in two** ("...assam" + "board.com") is erased whole, and
  **one garbled word between two URL words** ("http" + "\EEE." + "onlinebu.com") is
  taken as the URL's middle.
- **An ink smear boxed as a "word"** (464 px beside an MP Board URL) is not content:
  boxes taller than four lines are ignored when checking what a URL touches.
- **Grey scans** (Barkatullah): global Otsu turned the grey area black and read 2
  words of a 250-word page. A page that reads almost nothing is read again with
  Sauvola thresholding and the better reading wins; the cleaned page is verified
  with the same threshold.
- **Our own "paperwala.org" mark** misread by OCR as an unknown site was 6% of the
  review queue; words inside the mark's boxes are ignored when verifying.

### Database read budget (2026-10-29)

On 2026-10-28 the pipeline read 700M Turso rows in a day (free limit 500M a month)
and Turso blocked every database in the org, the live site's included. Rules since:

- **No query may scan a table per paper.** `claim` walks an index and stops at its
  LIMIT (`ix_papers_claim`); "any left?" checks stop at 1000 rows (`chain.CAP`);
  looking up which site row links a Drive file uses `publish.LinkIndex` (one read
  of `pyq_papers` per run) because `instr()` cannot use an index. Publish, sync and
  route all use it. `tests/test_read_budget.py` fails if any of this regresses.
- **Every run counts its reads and has a fuse.** Turso returns `rows_read` for each
  statement; `db.METER` adds them up, prints one line at exit
  (`database use this run: ...`) and raises `ReadBudgetExceeded` (a `DbError`, so a
  worker hands its paper back and stops) at `PIPELINE_READ_BUDGET` rows per process
  (default 500,000; `0` = off). Normal use is thousands of rows.
- **Keep the pipeline's state database in a different Turso account from the
  site's.** The free quota is per org; one over its limit blocks all of its databases.
- Still one pass over the table per run (not per paper): route's `_todo`, the daily
  `summary`/health counts. Measure with `turso plan show` after any change.

### Lessons from going live (2026-10-28)

- Groq is behind Cloudflare, which answers urllib's default User-Agent with `403 error code: 1010`; send a UA, or a 403 retires every key.
- Pinned Gemini model names get withdrawn for new keys (404); use the `gemini-flash-latest` alias.
- Hindi: `वार्षिक परीक्षा` (annual) is the tail of `अर्धवार्षिक परीक्षा` (half yearly); match half yearly first and guard annual.
- Turso rejects named SQL parameters a statement does not use; SQLite ignores them.
