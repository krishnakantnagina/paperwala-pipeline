"""Worker tests: real cleaning on one fixture, stubs for the failure paths."""

import hashlib
import shutil
from pathlib import Path

import pytest

from pwpipe import state, worker
from pwpipe.blocklist import Blocklist
from pwpipe.db import SqliteDb
from pwpipe.drive import DriveError, Uploaded

FIXTURES = Path(__file__).parent / "fixtures"
CHEM = FIXTURES / "mp_chem_2027.pdf"


def sha1(path):
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


class FakeDrive:
    def __init__(self, files, fail_download=None):
        self.files = files                    # drive_id -> local fixture path
        self.fail_download = fail_download
        self.uploads = []
        self.folders = []

    def download(self, file_id, dest):
        if self.fail_download:
            raise self.fail_download
        shutil.copyfile(self.files[file_id], dest)
        return sha1(dest), Path(dest).stat().st_size

    def ensure_folder(self, parts):
        self.folders.append(tuple(parts))
        return "/".join(parts)

    def upload(self, path, name, parent, source_id, sha256, anyone_reader=True,
               mime="application/pdf"):
        self.uploads.append({"name": name, "parent": parent, "source": source_id,
                             "public": anyone_reader, "mime": mime})
        return Uploaded(f"up-{len(self.uploads)}", "md5", Path(path).stat().st_size, False)


@pytest.fixture
def db(tmp_path):
    d = SqliteDb(str(tmp_path / "state.db"))
    state.migrate(d)
    yield d
    d.close()


def add_paper(db, drive_id, catalog_sha1, path="MP Board/Class 12/Final/Chemistry - 2027.pdf"):
    db.execute("""INSERT INTO papers (drive_id, catalog, path, catalog_sha1, updated_at)
                  VALUES (:d, 'mpboardonline.com', :p, :s, 1)""",
               {"d": drive_id, "p": path, "s": catalog_sha1})


@pytest.fixture
def bl():
    return Blocklist.load()


@pytest.fixture(autouse=True)
def no_sampling(monkeypatch):
    monkeypatch.setattr(worker, "AUTO_SAMPLE_RATE", 0.0)


def test_real_paper_is_cleaned_uploaded_privately_and_approved(db, bl):
    add_paper(db, "chem", sha1(CHEM))
    drive = FakeDrive({"chem": CHEM})
    stats = worker.run(db, drive, bl, "mpboardonline.com", limit=5, max_minutes=30,
                       owner="t", log=lambda *_: None)
    assert (stats.claimed, stats.approved, stats.review, stats.failed) == (1, 1, 0, 0)

    row = state.get(db, "chem")
    assert row["stage"] == "approved" and row["status"] == "ready"
    assert row["output_file_id"] and row["report_ref"] and row["auto_ok"] == 1
    pdf = next(u for u in drive.uploads if u["mime"] == "application/pdf")
    assert pdf["public"] is False                       # private until publish
    assert pdf["name"] == "Chemistry - 2027.pdf"
    assert pdf["parent"] == "PYQ/MP Board/Class 12/Final"
    assert all(not u["public"] for u in drive.uploads)


@pytest.fixture
def stub_process(monkeypatch):
    def fake(src, dst, bl, paper_id="", crops_dir=None, langs=None):
        shutil.copyfile(src, dst)
        return {"review": [], "auto_ok": True, "source_sha256": "s", "output_sha256": "o"}
    monkeypatch.setattr(worker, "process_paper", fake)


def test_pilot_sends_everything_to_review(db, bl, stub_process):
    add_paper(db, "p1", sha1(CHEM))
    stats = worker.run(db, FakeDrive({"p1": CHEM}), bl, None, 5, 30, "t", review_all=True,
                       log=lambda *_: None)
    row = state.get(db, "p1")
    assert stats.review == 1 and row["stage"] == "staged" and row["status"] == "review"
    assert "pilot" in row["review_reasons"]


def test_changed_original_fails_the_paper_not_the_run(db, bl, stub_process):
    add_paper(db, "bad", "0" * 40)
    add_paper(db, "good", sha1(CHEM))
    stats = worker.run(db, FakeDrive({"bad": CHEM, "good": CHEM}), bl, None, 5, 30, "t",
                       log=lambda *_: None)
    assert stats.failed == 1 and stats.approved == 1
    bad = state.get(db, "bad")
    assert bad["status"] == "ready" and bad["attempts"] == 1
    assert "original changed on Drive" in bad["last_error"]


def test_unreadable_pdf_fails_the_paper(db, bl, tmp_path):
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"not a pdf at all")
    add_paper(db, "junk", sha1(junk))
    stats = worker.run(db, FakeDrive({"junk": junk}), bl, None, 5, 30, "t", log=lambda *_: None)
    assert stats.failed == 1
    assert state.get(db, "junk")["attempts"] == 1


def test_lost_drive_login_stops_run_and_frees_paper(db, bl, stub_process):
    add_paper(db, "p1", sha1(CHEM))
    add_paper(db, "p2", sha1(CHEM))
    drive = FakeDrive({}, fail_download=DriveError("token refresh failed (400): invalid_grant", 400))
    with pytest.raises(worker.InfraError):
        worker.run(db, drive, bl, None, 5, 30, "t", log=lambda *_: None)
    rows = db.execute("SELECT status, attempts FROM papers")
    # The paper being worked is released untouched; no attempt is charged.
    assert all(r["attempts"] == 0 for r in rows)
    assert any(r["status"] == "ready" for r in rows)


def test_single_file_403_is_the_papers_problem(db, bl, stub_process):
    add_paper(db, "p1", sha1(CHEM))
    drive = FakeDrive({}, fail_download=DriveError("GET -> 403: cannotDownloadFile", 403))
    stats = worker.run(db, drive, bl, None, 5, 30, "t", log=lambda *_: None)
    assert stats.failed == 1


def test_out_of_time_claims_nothing(db, bl, stub_process):
    add_paper(db, "p1", sha1(CHEM))
    stats = worker.run(db, FakeDrive({"p1": CHEM}), bl, None, 5, max_minutes=1, owner="t",
                       log=lambda *_: None)
    assert stats.claimed == 0
    assert state.get(db, "p1")["status"] == "ready"
