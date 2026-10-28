import json
from pathlib import Path

import pytest

from pwpipe import route, route_job, state, sync
from pwpipe.db import SqliteDb
from pwpipe.keys import QuotaExhausted

BIO = {"id": 10, "name": "Biology", "code": "", "aliases": ""}
CHEM = {"id": 11, "name": "Chemistry", "code": "", "aliases": ""}
PHYS = {"id": 12, "name": "Physics", "code": "", "aliases": ""}


def test_rules_read_session_type_and_month():
    f = route.rule_facts("Quarterly Examination 2026-27\nSubject:- Biology  Class - 12th  MM:70")
    assert (f.year, f.exam_type) == (2027, "Quarterly")
    f = route.rule_facts("अर्धवार्षिक परीक्षा 2024 विषय: गणित")
    assert (f.year, f.exam_type) == (2024, "Half Yearly")
    f = route.rule_facts("B.Tech Semester Examination Nov 2019 CS-302 Data Structures")
    assert f.month == "Nov" and f.codes == ["CS-302"] and f.year == 2019


def test_subject_seen_by_name_alias_or_code():
    assert route.subject_seen("Subject:- Biology Class 12", BIO)
    assert route.subject_seen("Paper CS 302 Data Str.", {"name": "DSA", "code": "CS-302", "aliases": ""})
    assert route.subject_seen("Data Structure", {"name": "DSA", "code": "", "aliases": "Data Structure"})
    assert not route.subject_seen("Subject:- Chemistry", BIO)


class FakeAI:
    def __init__(self, answer=None, exhausted=False):
        self.answer, self.exhausted, self.calls = answer, exhausted, 0

    def read(self, header, image_png=None):
        self.calls += 1
        if self.exhausted:
            raise QuotaExhausted("out")
        return self.answer


def test_confirmed_subject_needs_no_ai_and_fills_only_empty_fields():
    ai = FakeAI()
    plan = route.plan_paper("Quarterly Examination 2026-27 Subject:- Biology",
                            {"year": None, "exam_type": ""}, BIO, [BIO, CHEM], ai)
    assert plan.confirmed and ai.calls == 0
    assert plan.fills == {"exam_type": "Quarterly", "year": 2027}
    plan = route.plan_paper("Quarterly Examination 2026-27 Subject:- Biology",
                            {"year": 2020, "exam_type": "Board Exam"}, BIO, [BIO], ai)
    assert plan.fills == {}                          # never overwrites the site


def test_move_proposed_only_to_an_existing_subject():
    ai = FakeAI({"subject": "Chemistry", "paper_code": "", "year": 2024, "exam_type": "",
                 "confident": True})
    plan = route.plan_paper("गड़बड़ OCR text", {"year": 2024, "exam_type": "x"}, BIO,
                            [BIO, CHEM, PHYS], ai)
    assert plan.move_to and plan.move_to["id"] == CHEM["id"]
    # A subject the site does not have is never proposed.
    ai = FakeAI({"subject": "Astrophysics", "paper_code": "", "year": None, "exam_type": "",
                 "confident": True})
    plan = route.plan_paper("noise", {"year": 1, "exam_type": "x"}, BIO, [BIO, CHEM], ai)
    assert plan.move_to is None
    # Unsure answers never move anything.
    ai = FakeAI({"subject": "Chemistry", "paper_code": "", "year": None, "exam_type": "",
                 "confident": False})
    assert route.plan_paper("noise", {"year": 1, "exam_type": "x"}, BIO, [BIO, CHEM], ai).move_to is None


def test_ai_tries_groq_then_gemini_and_rotates_keys():
    calls = []

    def post(url, body, headers, timeout=60):
        calls.append(url)
        if "groq" in url:
            return 429, {"retry_after": 30}
        return 200, {"candidates": [{"content": {"parts": [{"text": json.dumps(
            {"subject": "Physics", "paper_code": "", "year": 2023, "exam_type": "Annual",
             "confident": True})}]}}]}

    from pwpipe.keys import KeyPool
    ai = route.AI(KeyPool.from_env("groq", 10, env={"GROQ_API_KEY": "g1"}),
                  KeyPool.from_env("gemini", 10, env={"GEMINI_API_KEY": "m1"}), post=post)
    answer = ai.read("header")
    assert answer["subject"] == "Physics"
    assert "groq" in calls[0] and "generativelanguage" in calls[1]
    assert ai.groq.keys[0].parked_until > 0          # the 429 parked the Groq key


# --- the job ---------------------------------------------------------------------

class FakeDrive:
    def __init__(self, report):
        self.report = report

    def download(self, file_id, dest):
        Path(dest).write_text(json.dumps(self.report), encoding="utf-8")
        return "sha", 1


def report_with_header(text):
    lines = [{"text": t, "bbox": [0, i * 40, 500, i * 40 + 30], "kind": None}
             for i, t in enumerate(text.split("\n"))]
    return {"pages": [{"raster_size": [1000, 1400], "lines": lines}]}


@pytest.fixture
def dbs(tmp_path):
    st = SqliteDb(str(tmp_path / "state.db"))
    state.migrate(st)
    live = SqliteDb(str(tmp_path / "live.db"))
    live.batch([
        ("""CREATE TABLE subjects (id INTEGER PRIMARY KEY, university TEXT, program TEXT,
            branch TEXT, semester INTEGER, code TEXT, name TEXT, aliases TEXT)""", None),
        ("""CREATE TABLE pyq_papers (id INTEGER PRIMARY KEY, subject_id INTEGER, year INTEGER,
            exam_type TEXT, title TEXT, drive_link TEXT, preview_link TEXT)""", None),
        ("""CREATE TABLE pipeline_review (id INTEGER PRIMARY KEY, source_id TEXT UNIQUE,
            pyq_paper_id INTEGER, university TEXT DEFAULT '', title TEXT DEFAULT '',
            kind TEXT DEFAULT 'clean', reasons TEXT, original_link TEXT DEFAULT '',
            cleaned_link TEXT DEFAULT '', crops TEXT, detail TEXT, decision TEXT DEFAULT '',
            decision_note TEXT DEFAULT '', decided_by TEXT DEFAULT '', decided_at TEXT,
            synced INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT)""", None),
        ("""CREATE TABLE paper_reports (id INTEGER PRIMARY KEY, pyq_paper_id INTEGER,
            reason TEXT, decision TEXT DEFAULT '', decided_by TEXT DEFAULT '',
            synced INTEGER DEFAULT 0)""", None),
        ("INSERT INTO subjects VALUES (10, 'MPBSE', 'Class 12', '', 0, '', 'Biology', '')", None),
        ("INSERT INTO subjects VALUES (11, 'MPBSE', 'Class 12', '', 0, '', 'Chemistry', '')", None),
        ("""INSERT INTO pyq_papers VALUES (1, 10, NULL, '', 'Biology - 2027',
            'https://drive.google.com/file/d/NEWCOPY_xxxxxxxxxxxxxxx/view', '')""", None),
    ])
    st.execute("""INSERT INTO papers (drive_id, catalog, stage, status, output_file_id, report_ref,
                  updated_at) VALUES ('ORIG_aaaaaaaaaaaaaaaaaaaa', 'c', 'published', 'done',
                  'NEWCOPY_xxxxxxxxxxxxxxx', 'REPORT1', 1)""")
    return st, live


def test_job_fills_empty_fields_from_rules(dbs):
    st, live = dbs
    drive = FakeDrive(report_with_header("Quarterly Examination 2026-27\nSubject:- Biology"))
    s = route_job.run(st, live, drive, FakeAI(), 10, 5, log=lambda *_: None)
    assert (s.checked, s.confirmed, s.filled, s.ai_calls) == (1, 1, 2, 0)
    row = live.execute("SELECT year, exam_type FROM pyq_papers")[0]
    assert (row["year"], row["exam_type"]) == (2027, "Quarterly")
    assert route_job.run(st, live, drive, FakeAI(), 10, 5, log=lambda *_: None).checked == 0


def test_job_proposes_move_and_sync_applies_it_after_approval(dbs):
    st, live = dbs
    drive = FakeDrive(report_with_header("Annual Examination 2024\nविषय : रसायन"))
    ai = FakeAI({"subject": "Chemistry", "paper_code": "", "year": 2024, "exam_type": "Annual",
                 "confident": True})
    s = route_job.run(st, live, drive, ai, 10, 5, log=lambda *_: None)
    assert s.proposed == 1
    item = live.execute("SELECT * FROM pipeline_review")[0]
    assert item["kind"] == "route" and json.loads(item["detail"])["to_subject_id"] == 11
    assert live.execute("SELECT subject_id FROM pyq_papers")[0]["subject_id"] == 10   # not yet
    live.execute("UPDATE pipeline_review SET decision = 'approve', decided_by = 'owner'")
    stats = sync.run(st, live, drive, log=lambda *_: None)
    assert stats.moved == 1
    assert live.execute("SELECT subject_id FROM pyq_papers")[0]["subject_id"] == 11


def test_job_keeps_going_with_rules_when_quota_runs_out(dbs):
    st, live = dbs
    drive = FakeDrive(report_with_header("Annual Examination 2024\nnoisy header"))
    s = route_job.run(st, live, drive, FakeAI(exhausted=True), 10, 5, log=lambda *_: None)
    assert s.waiting_quota == 1 and s.filled == 2     # rule fills still applied
    assert st.execute("SELECT status FROM routing")[0]["status"] == "waiting"


def test_waiting_never_uses_up_attempts(dbs):
    st, live = dbs
    drive = FakeDrive(report_with_header("Annual Examination 2024\nnoisy header"))
    for _ in range(5):
        route_job.run(st, live, drive, FakeAI(exhausted=True), 10, 5, log=lambda *_: None)
    row = st.execute("SELECT status, attempts FROM routing")[0]
    assert (row["status"], row["attempts"]) == ("waiting", 0)


def test_moves_are_held_until_the_site_review_table_exists(dbs):
    st, live = dbs
    live.execute("DROP TABLE pipeline_review")
    drive = FakeDrive(report_with_header("Annual Examination 2024\nविषय : रसायन"))
    ai = FakeAI({"subject": "Chemistry", "paper_code": "", "year": 2024, "exam_type": "Annual",
                 "confident": True})
    s = route_job.run(st, live, drive, ai, 10, 5, log=lambda *_: None)
    assert s.waiting_site == 1 and s.filled == 2
    assert st.execute("SELECT status FROM routing")[0]["status"] == "waiting"
