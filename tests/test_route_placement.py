"""route_job with the placement engine on (flags), against a small site database."""
import json
from pathlib import Path

import pytest

from pwpipe import route_job, state
from pwpipe.db import SqliteDb
from pwpipe.route_job import Flags

CBCS = """CE-221-CBCS
B.E., II Semester Examination, June 2020
Choice Based Credit System (CBCS)
Fluid Mechanics
Time : Three Hours"""

STRANGER = """XX-999
B.E. IV Semester Examination, June 2020
Choice Based Grading System (CBGS)
Zzz Qqq Www
Time : Three Hours"""


class FakeDrive:
    def __init__(self, text):
        lines = [{"text": t, "bbox": [0, i * 40, 500, i * 40 + 30], "kind": None} for i, t in enumerate(text.split("\n"))]
        self.report = {"pages": [{"raster_size": [1000, 1400], "lines": lines}]}

    def download(self, file_id, dest):
        Path(dest).write_text(json.dumps(self.report), encoding="utf-8")
        return "sha", 1


def make(tmp_path, with_scheme=True, unsorted=True):
    st = SqliteDb(str(tmp_path / "state.db"))
    state.migrate(st)
    live = SqliteDb(str(tmp_path / "live.db"))
    scheme_col = ", scheme TEXT DEFAULT ''" if with_scheme else ""
    stmts = [
        (f"""CREATE TABLE subjects (id INTEGER PRIMARY KEY, university TEXT, program TEXT, branch TEXT,
            semester INTEGER, code TEXT, name TEXT, aliases TEXT{scheme_col})""", None),
        ("""CREATE TABLE pyq_papers (id INTEGER PRIMARY KEY, subject_id INTEGER, year INTEGER, exam_type TEXT,
            medium TEXT DEFAULT '', title TEXT, drive_link TEXT, preview_link TEXT, file_size TEXT DEFAULT '')""", None),
        ("""CREATE TABLE pipeline_review (id INTEGER PRIMARY KEY, source_id TEXT UNIQUE, pyq_paper_id INTEGER,
            university TEXT DEFAULT '', title TEXT DEFAULT '', kind TEXT DEFAULT 'clean', reasons TEXT,
            original_link TEXT DEFAULT '', cleaned_link TEXT DEFAULT '', crops TEXT, detail TEXT,
            decision TEXT DEFAULT '', decision_note TEXT DEFAULT '', decided_by TEXT DEFAULT '', decided_at TEXT,
            synced INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT)""", None),
        ("""CREATE TABLE paper_reports (id INTEGER PRIMARY KEY, pyq_paper_id INTEGER, reason TEXT,
            decision TEXT DEFAULT '', decided_by TEXT DEFAULT '', synced INTEGER DEFAULT 0)""", None),
    ]
    if unsorted:
        stmts.append(("""CREATE TABLE unsorted_papers (id INTEGER PRIMARY KEY, old_paper_id INTEGER, old_subject_id INTEGER,
            old_subject_name TEXT, university TEXT, program TEXT, year INTEGER, exam_type TEXT, medium TEXT,
            title TEXT, drive_link TEXT, preview_link TEXT, file_size TEXT, reason TEXT, detail TEXT,
            header_text TEXT, created_at TEXT)""", None))
    rows = [(10, "CE-221", "Fluid Mechanics", "CBGS"), (11, "CE-221", "Fluid Mechanics", "CBCS"),
            (12, "", "Ce Fluid Mechanics", "")]
    for sid, code, name, scheme in rows:
        if with_scheme:
            stmts.append(("INSERT INTO subjects VALUES (:i, 'RGPV', 'B.Tech', 'Civil', 2, :c, :n, '', :s)",
                          {"i": sid, "c": code, "n": name, "s": scheme}))
        else:
            stmts.append(("INSERT INTO subjects VALUES (:i, 'RGPV', 'B.Tech', 'Civil', 2, :c, :n, '')",
                          {"i": sid, "c": code, "n": name}))
    stmts.append(("INSERT INTO pyq_papers (id, subject_id, year, exam_type, title, drive_link, preview_link) VALUES "
                  "(1, 12, NULL, '', 'Ce Fluid Mechanics - Jun 2020', 'https://drive.google.com/file/d/NEWCOPY_xxxxxxxxxxxxxxx/view', '')", None))
    live.batch(stmts)
    st.execute("""INSERT INTO papers (drive_id, catalog, stage, status, output_file_id, report_ref, updated_at)
                  VALUES ('ORIG_aaaaaaaaaaaaaaaaaaaa', 'c', 'published', 'done', 'NEWCOPY_xxxxxxxxxxxxxxx', 'REPORT1', 1)""")
    return st, live


def run(st, live, text, **flags):
    return route_job.run(st, live, FakeDrive(text), None, 10, 5, log=lambda *_: None, flags=Flags(placement=True, **flags))


def test_move_is_a_proposal_by_default_and_the_paper_stays(tmp_path):
    st, live = make(tmp_path)
    s = run(st, live, CBCS)
    assert (s.reviews, s.moved) == (1, 0)
    item = live.execute("SELECT * FROM pipeline_review")[0]
    assert item["kind"] == "route" and json.loads(item["detail"])["to_subject_id"] == 11
    assert live.execute("SELECT subject_id FROM pyq_papers")[0]["subject_id"] == 12


def test_auto_move_files_the_paper_under_the_subject_the_header_names(tmp_path):
    st, live = make(tmp_path)
    s = run(st, live, CBCS, auto_move=True)
    assert (s.moved, s.reviews) == (1, 0)
    assert live.execute("SELECT subject_id FROM pyq_papers")[0]["subject_id"] == 11
    assert live.execute("SELECT COUNT(*) AS n FROM pipeline_review")[0]["n"] == 0


def test_a_paper_that_matches_its_subject_is_kept_and_asks_for_nothing(tmp_path):
    st, live = make(tmp_path)
    live.execute("UPDATE pyq_papers SET subject_id = 11")
    s = run(st, live, CBCS)
    assert (s.confirmed, s.reviews, s.moved, s.hidden) == (1, 0, 0, 0)


def test_unplaceable_paper_goes_to_review_unless_hiding_is_on(tmp_path):
    st, live = make(tmp_path)
    live.execute("UPDATE pyq_papers SET subject_id = 10")
    s = run(st, live, STRANGER)
    assert (s.reviews, s.hidden) == (1, 0)
    assert live.execute("SELECT COUNT(*) AS n FROM pyq_papers")[0]["n"] == 1


def test_hiding_keeps_the_row_in_unsorted_papers_and_takes_it_off_the_site(tmp_path):
    st, live = make(tmp_path)
    live.execute("UPDATE pyq_papers SET subject_id = 10")
    s = run(st, live, STRANGER, hide_unsorted=True)
    assert s.hidden == 1
    assert live.execute("SELECT COUNT(*) AS n FROM pyq_papers")[0]["n"] == 0
    row = live.execute("SELECT * FROM unsorted_papers")[0]
    assert (row["old_paper_id"], row["old_subject_id"], row["reason"]) == (1, 10, "code_unknown")
    assert "XX-999" in row["header_text"] and row["drive_link"].endswith("/view")


def test_unverified_paper_is_hidden_only_when_asked_and_kept_in_unsorted_papers(tmp_path):
    st, live = make(tmp_path)
    live.execute("UPDATE pyq_papers SET subject_id = 10")
    s = run(st, live, "Nothing Useful Here")
    assert (s.unverified, s.hidden) == (1, 0)
    assert live.execute("SELECT COUNT(*) AS n FROM pyq_papers")[0]["n"] == 1
    (tmp_path / "b").mkdir()
    st2, live2 = make(tmp_path / "b")
    live2.execute("UPDATE pyq_papers SET subject_id = 10")
    s = run(st2, live2, "Nothing Useful Here", hide_unverified=True)
    assert (s.unverified, s.hidden) == (1, 1)
    assert live2.execute("SELECT COUNT(*) AS n FROM pyq_papers")[0]["n"] == 0
    assert live2.execute("SELECT COUNT(*) AS n FROM unsorted_papers")[0]["n"] == 1


def test_hiding_needs_the_unsorted_table_else_it_falls_back_to_review(tmp_path):
    st, live = make(tmp_path, unsorted=False)
    live.execute("UPDATE pyq_papers SET subject_id = 10")
    s = run(st, live, STRANGER, hide_unsorted=True)
    assert (s.hidden, s.reviews) == (0, 1)
    assert live.execute("SELECT COUNT(*) AS n FROM pyq_papers")[0]["n"] == 1


def test_site_without_the_scheme_column_still_routes(tmp_path):
    st, live = make(tmp_path, with_scheme=False)
    s = run(st, live, CBCS)
    assert s.errors == 0 and s.reviews + s.moved + s.confirmed == 1


def test_flags_default_to_off(monkeypatch):
    for k in ("ROUTE_PLACEMENT", "ROUTE_AUTO_MOVE", "ROUTE_HIDE_UNSORTED"):
        monkeypatch.delenv(k, raising=False)
    f = Flags.from_env()
    assert (f.placement, f.auto_move, f.hide_unsorted) == (False, False, False)
    monkeypatch.setenv("ROUTE_PLACEMENT", "1")
    assert Flags.from_env().placement is True


def test_approving_an_unsorted_proposal_takes_the_paper_off_the_site(tmp_path):
    from pwpipe import sync
    st, live = make(tmp_path)
    live.execute("UPDATE pyq_papers SET subject_id = 10")
    run(st, live, STRANGER)                       # proposal only: the paper stays
    assert live.execute("SELECT COUNT(*) AS n FROM pyq_papers")[0]["n"] == 1
    item = live.execute("SELECT * FROM pipeline_review")[0]
    assert json.loads(item["detail"])["placement"] == "unsorted"
    live.execute("UPDATE pipeline_review SET decision = 'approve', decided_by = 'owner'")
    stats = sync.run(st, live, FakeDrive(STRANGER), log=lambda *_: None)
    assert stats.hidden == 1
    assert live.execute("SELECT COUNT(*) AS n FROM pyq_papers")[0]["n"] == 0
    assert live.execute("SELECT old_subject_id, reason FROM unsorted_papers")[0] == {"old_subject_id": 10, "reason": "code_unknown"}


def test_approving_a_review_without_a_target_changes_nothing(tmp_path):
    from pwpipe import sync
    st, live = make(tmp_path)
    live.execute("UPDATE pyq_papers SET subject_id = 10, title = 'x'")
    live.execute("UPDATE subjects SET scheme = 'CBGS' WHERE id = 10")
    run(st, live, CBCS)          # code matches subject 10 (CBGS) but the header says CBCS: sibling 11 exists -> a move proposal
    live.execute("DELETE FROM pipeline_review")
    live.execute("INSERT INTO pipeline_review (source_id, pyq_paper_id, university, kind, reasons, detail, decision, decided_by) "
                 "VALUES ('route:z', 1, 'RGPV', 'route', '[\"placement_review\"]', "
                 "'{\"from_subject_id\": 10, \"to_subject_id\": null, \"placement\": \"review\"}', 'approve', 'owner')")
    sync.run(st, live, FakeDrive(CBCS), log=lambda *_: None)
    assert live.execute("SELECT subject_id FROM pyq_papers")[0]["subject_id"] == 10
