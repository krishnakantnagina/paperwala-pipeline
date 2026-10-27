"""State machine tests on a local SQLite file (same SQL as Turso)."""

import csv
import os

import pytest

from pwpipe import state
from pwpipe.db import SqliteDb, TursoDb


class Clock:
    def __init__(self):
        self.t = 1_800_000_000

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(state, "now", c)
    return c


TEST_URL = os.environ.get("PIPELINE_TEST_DB_URL")
BACKENDS = ["sqlite"] + (["turso"] if TEST_URL else [])


@pytest.fixture(params=BACKENDS)
def db(request, tmp_path):
    if request.param == "sqlite":
        d = SqliteDb(str(tmp_path / "state.db"))
    else:
        # The suite wipes its tables, so it must never point at the real
        # pipeline database.
        assert "-test-" in TEST_URL, "PIPELINE_TEST_DB_URL must be the -test database"
        d = TursoDb(TEST_URL, os.environ["PIPELINE_TEST_DB_TOKEN"])
        for table in ("events", "papers", "meta"):
            d.execute(f"DROP TABLE IF EXISTS {table}")
    state.migrate(d)
    yield d
    d.close()


def make_catalog(path, n=5, missing_ids=1):
    path.mkdir()
    header = ["title", "code", "session", "month", "year", "university", "course", "branch",
              "group", "file", "path", "view", "preview", "download", "driveId"]
    with open(path / "papers_full.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for i in range(n + missing_ids):
            drive = f"id{i}" if i < n else ""
            w.writerow([f"Biology {i}", "", "2027", "", "2027", "MP Board", "Class 12", "",
                        "Trimasik", f"b{i}.pdf", f"MP Board/Class 12/b{i}.pdf", "", "", "", drive])
    with open(path / "arranged_manifest.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["arranged_path", "sha1", "bytes"])
        for i in range(n):
            w.writerow([f"MP Board/Class 12/b{i}.pdf", f"sha{i}", 1000 + i])
    return path


@pytest.fixture
def seeded(db, tmp_path, clock):
    state.seed_catalog(db, make_catalog(tmp_path / "cat"), "mpboardonline.com")
    return db


def expire_leases(db, clock):
    clock.t += state.LEASE_SECONDS + 1


def test_migrate_is_idempotent(db):
    state.migrate(db)
    assert db.execute("SELECT value FROM meta WHERE key='schema_version'")[0]["value"] ==         str(state.SCHEMA_VERSION)
    columns = {r["name"] for r in db.execute("PRAGMA table_info(papers)")}
    assert {"output_file_id", "output_bytes", "crops_ref"} <= columns


def test_upgrade_from_v1_keeps_rows(tmp_path, clock):
    d = SqliteDb(str(tmp_path / "v1.db"))
    d.batch([(sql, None) for sql in state._SCHEMA] +
            [("INSERT INTO meta VALUES ('schema_version', '1')", None),
             ("INSERT INTO papers (drive_id, catalog, updated_at) VALUES ('x', 'c', 1)", None)])
    state.migrate(d)
    assert state.get(d, "x")["output_file_id"] is None
    assert d.execute("SELECT value FROM meta WHERE key='schema_version'")[0]["value"] ==         str(state.SCHEMA_VERSION)


def test_seed_counts_and_joins_manifest(db, tmp_path, clock):
    stats = state.seed_catalog(db, make_catalog(tmp_path / "cat"), "mpboardonline.com")
    assert (stats.rows, stats.inserted, stats.no_drive_id) == (6, 5, 1)
    row = state.get(db, "id3")
    assert row["catalog_sha1"] == "sha3" and row["catalog_bytes"] == 1003
    assert row["stage"] == "discovered" and row["status"] == "ready"


def test_reseed_never_resets_progress(seeded, tmp_path, clock):
    paper = state.claim(seeded, "discovered", "A", 1)[0]
    assert state.advance(seeded, paper, "A", "downloaded", source_sha256="x")
    stats = state.seed_catalog(seeded, tmp_path / "cat", "mpboardonline.com")
    assert stats.inserted == 0
    assert state.get(seeded, paper["drive_id"])["stage"] == "downloaded"


def test_claims_never_overlap(seeded, clock):
    a = state.claim(seeded, "discovered", "A", 3)
    b = state.claim(seeded, "discovered", "B", 10)
    assert len(a) == 3 and len(b) == 2
    assert not {p["drive_id"] for p in a} & {p["drive_id"] for p in b}
    assert state.claim(seeded, "discovered", "C", 10) == []


def test_stale_worker_cannot_overwrite(seeded, clock):
    paper = state.claim(seeded, "discovered", "A", 1)[0]
    expire_leases(seeded, clock)
    retaken = state.claim(seeded, "discovered", "B", 1)[0]
    assert retaken["drive_id"] == paper["drive_id"]
    assert retaken["attempts"] == 1          # the dead lease used an attempt
    assert not state.advance(seeded, paper, "A", "downloaded", source_sha256="stale")
    assert state.advance(seeded, retaken, "B", "downloaded", source_sha256="fresh")
    assert state.get(seeded, paper["drive_id"])["source_sha256"] == "fresh"


def test_advance_retry_after_lost_reply_is_success(seeded, clock):
    paper = state.claim(seeded, "discovered", "A", 1)[0]
    assert state.advance(seeded, paper, "A", "downloaded", source_sha256="h")
    # Same call again, as a network retry would make it.
    assert state.advance(seeded, paper, "A", "downloaded", source_sha256="h")
    events = seeded.execute("SELECT * FROM events WHERE drive_id = :d", {"d": paper["drive_id"]})
    assert len(events) == 1


def test_failure_backs_off_then_dies(seeded, clock):
    paper = state.claim(seeded, "discovered", "A", 1)[0]
    drive_id = paper["drive_id"]
    for attempt in range(1, state.MAX_ATTEMPTS + 1):
        assert state.fail(seeded, paper, "A", f"boom {attempt}")
        row = state.get(seeded, drive_id)
        if attempt < state.MAX_ATTEMPTS:
            assert row["status"] == "ready" and row["lease_until"] > clock.t
            # Backing off: not claimable yet...
            assert drive_id not in {p["drive_id"] for p in
                                    state.claim(seeded, "discovered", "X", 10)}
            for p in seeded.execute("SELECT * FROM papers WHERE lease_owner = 'X'"):
                state.release(seeded, p, "X")
            # ...but claimable once the backoff passes.
            clock.t = row["lease_until"] + 1
            paper = next(p for p in state.claim(seeded, "discovered", "A", 10)
                         if p["drive_id"] == drive_id)
        else:
            assert row["status"] == "dead" and row["last_error"] == "boom 5"


def test_paper_that_kills_its_worker_ends_dead(seeded, clock):
    drive_id = None
    for _ in range(state.MAX_ATTEMPTS):
        papers = state.claim(seeded, "discovered", "W", 10)
        if drive_id is None:
            drive_id = papers[0]["drive_id"]
        for p in papers:
            if p["drive_id"] != drive_id:
                state.release(seeded, p, "W")
        expire_leases(seeded, clock)       # worker crashes holding it
    state.claim(seeded, "discovered", "W", 10)
    row = state.get(seeded, drive_id)
    assert row["status"] == "dead" and row["last_error"] == state.LEASE_EXPIRED


def test_renew_only_extends_own_leases(seeded, clock):
    a = state.claim(seeded, "discovered", "A", 2)
    ids = [p["drive_id"] for p in a]
    assert state.renew(seeded, "A", ids) == 2
    assert state.renew(seeded, "B", ids) == 0


def test_review_actions_are_fenced(seeded, clock):
    paper = state.claim(seeded, "discovered", "A", 1)[0]
    assert state.advance(seeded, paper, "A", "processed", status="review",
                         review_reasons=["suspect"])
    d = paper["drive_id"]
    assert state.get(seeded, d)["review_reasons"] == '["suspect"]'
    assert not state.set_status(seeded, d, "processed", "ready", "ready", "owner")
    assert state.set_status(seeded, d, "processed", "review", "ready", "owner", "approved")
    assert not state.set_status(seeded, d, "processed", "review", "ready", "owner")


def test_bad_transitions_rejected(seeded, clock):
    paper = state.claim(seeded, "discovered", "A", 1)[0]
    with pytest.raises(ValueError):
        state.advance(seeded, paper, "A", "nowhere")
    with pytest.raises(ValueError):
        state.advance(seeded, paper, "A", "downloaded", status="leased")
    with pytest.raises(ValueError):
        state.advance(seeded, paper, "A", "downloaded", stage="published")
