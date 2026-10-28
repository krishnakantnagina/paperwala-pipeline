"""Sync tests: SQLite stands in for the live site DB with the 0009 tables."""

import json

import pytest

from pwpipe import publish, state, sync
from pwpipe.db import SqliteDb


class FakeDrive:
    def __init__(self):
        self.shared = []

    def list_folder(self, folder):
        return [{"id": f"{folder}-crop1", "name": "p1_r1.png"}]

    def share_anyone(self, fid):
        self.shared.append(fid)


@pytest.fixture
def dbs(tmp_path):
    st = SqliteDb(str(tmp_path / "state.db"))
    state.migrate(st)
    live = SqliteDb(str(tmp_path / "live.db"))
    live.batch([
        ("CREATE TABLE subjects (id INTEGER PRIMARY KEY, university TEXT, name TEXT)", None),
        ("CREATE TABLE pyq_papers (id INTEGER PRIMARY KEY, subject_id INTEGER, title TEXT, "
         "drive_link TEXT, preview_link TEXT)", None),
        ("""CREATE TABLE pipeline_review (id INTEGER PRIMARY KEY, source_id TEXT UNIQUE,
            pyq_paper_id INTEGER, university TEXT DEFAULT '', title TEXT DEFAULT '',
            kind TEXT DEFAULT 'clean', reasons TEXT, original_link TEXT DEFAULT '',
            cleaned_link TEXT DEFAULT '', crops TEXT, detail TEXT, decision TEXT DEFAULT '',
            decision_note TEXT DEFAULT '', decided_by TEXT DEFAULT '', decided_at TEXT,
            synced INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT)""", None),
        ("""CREATE TABLE paper_reports (id INTEGER PRIMARY KEY, pyq_paper_id INTEGER,
            reason TEXT, note TEXT DEFAULT '', ip_hash TEXT DEFAULT '',
            decision TEXT DEFAULT '', decided_by TEXT DEFAULT '', decided_at TEXT,
            synced INTEGER DEFAULT 0, created_at TEXT)""", None),
        ("INSERT INTO subjects VALUES (1, 'MPBSE', 'Biology')", None),
    ])
    return st, live


def add(st, live, orig, stage="staged", status="review", new="NEW_" + "x" * 20, on_site=True):
    st.execute("""INSERT INTO papers (drive_id, catalog, title, stage, status, output_file_id,
                  crops_ref, review_reasons, updated_at)
                  VALUES (:d, 'c', 'Biology 2027', :s, :st, :n, 'CROPS', :r, 5)""",
               {"d": orig, "s": stage, "st": status, "n": new,
                "r": json.dumps(["pilot", "suspect"])})
    if on_site:
        live.execute("INSERT INTO pyq_papers (subject_id, title, drive_link, preview_link) "
                     "VALUES (1, 'Biology - 2027', :v, :p)",
                     {"v": publish.view_link(orig), "p": publish.preview_link(orig)})


ORIG = "ORIG_" + "a" * 20


def test_push_shares_and_queues_once(dbs):
    st, live = dbs
    add(st, live, ORIG)
    drive = FakeDrive()
    s = sync.run(st, live, drive, log=lambda *_: None)
    assert s.pushed == 1
    row = live.execute("SELECT * FROM pipeline_review")[0]
    assert row["university"] == "MPBSE" and json.loads(row["reasons"]) == ["suspect"]
    assert json.loads(row["crops"])[0]["id"] == "CROPS-crop1"
    assert set(drive.shared) == {"CROPS-crop1", "NEW_" + "x" * 20}
    assert sync.run(st, live, drive, log=lambda *_: None).pushed == 0     # not pushed again


def test_paper_not_on_site_is_not_queued(dbs):
    st, live = dbs
    add(st, live, ORIG, on_site=False)
    s = sync.run(st, live, FakeDrive(), log=lambda *_: None)
    assert s.pushed == 0 and s.not_on_site == 1


@pytest.mark.parametrize("decision,expect", [
    ("approve", ("approved", "ready")),
    ("keep_original", ("staged", "done")),
    ("reclean", ("discovered", "ready")),
])
def test_decisions_are_applied_once(dbs, decision, expect):
    st, live = dbs
    add(st, live, ORIG)
    sync.run(st, live, FakeDrive(), log=lambda *_: None)
    live.execute("UPDATE pipeline_review SET decision = :d, decided_by = 'owner'", {"d": decision})
    sync.run(st, live, FakeDrive(), log=lambda *_: None)
    p = state.get(st, ORIG)
    assert (p["stage"], p["status"]) == expect
    assert live.execute("SELECT synced FROM pipeline_review")[0]["synced"] == 1
    events = st.execute("SELECT * FROM events WHERE owner = 'admin:owner'")
    assert len(events) == 1


def test_report_restore_original_unpublishes(dbs):
    st, live = dbs
    new = "NEW_" + "y" * 20
    add(st, live, ORIG, stage="published", status="done", new=new)
    live.execute("UPDATE pyq_papers SET drive_link = :v, preview_link = :p",
                 {"v": publish.view_link(new), "p": publish.preview_link(new)})
    live.execute("INSERT INTO paper_reports (pyq_paper_id, reason, decision, decided_by) "
                 "VALUES (1, 'text_missing', 'restore_original', 'owner')")
    s = sync.run(st, live, FakeDrive(), log=lambda *_: None)
    assert s.reports_restored == 1
    assert live.execute("SELECT drive_link FROM pyq_papers")[0]["drive_link"] == publish.view_link(ORIG)
    p = state.get(st, ORIG)
    assert (p["stage"], p["status"]) == ("staged", "done")
    assert live.execute("SELECT synced FROM paper_reports")[0]["synced"] == 1


def test_report_reclean_requeues(dbs):
    st, live = dbs
    add(st, live, ORIG, stage="approved", status="ready")
    live.execute("INSERT INTO paper_reports (pyq_paper_id, reason, decision, decided_by) "
                 "VALUES (1, 'ad_or_mark', 'reclean', 'owner')")
    s = sync.run(st, live, FakeDrive(), log=lambda *_: None)
    assert s.reports_recleaned == 1 and state.get(st, ORIG)["stage"] == "discovered"


def test_without_site_tables_nothing_happens(tmp_path):
    st = SqliteDb(str(tmp_path / "s.db"))
    live = SqliteDb(str(tmp_path / "l.db"))
    s = sync.run(st, live, FakeDrive(), log=lambda *_: None)
    assert s.pushed == 0


def test_entry_for_a_recleaned_paper_is_dropped(dbs):
    st, live = dbs
    add(st, live, ORIG)
    sync.run(st, live, FakeDrive(), log=lambda *_: None)
    state.requeue(st, "t", "new rules", drive_ids=[ORIG])
    s = sync.run(st, live, FakeDrive(), log=lambda *_: None)
    assert s.dropped_stale == 1 and live.execute("SELECT COUNT(*) n FROM pipeline_review")[0]["n"] == 0
