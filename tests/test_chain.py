import pytest

from pwpipe import chain, route_job, state
from pwpipe.db import SqliteDb


@pytest.fixture
def db(tmp_path):
    db = SqliteDb(str(tmp_path / "s.db"))
    state.migrate(db)
    return db


def paper(db, pid, stage="discovered", status="ready", report=None):
    db.execute("INSERT INTO papers (drive_id, catalog, stage, status, report_ref, updated_at) "
               "VALUES (:d, 'c', :s, :st, :r, 1)",
               {"d": pid, "s": stage, "st": status, "r": report})


def event(db, pid, owner, to_stage, status, at=100):
    db.execute("INSERT INTO events (drive_id, at, owner, from_stage, to_stage, status) "
               "VALUES (:d, :a, :o, 'x', :t, :s)",
               {"d": pid, "a": at, "o": owner, "t": to_stage, "s": status})


def test_work_runs_again_while_papers_left_and_this_run_moved_some(db):
    paper(db, "a")
    paper(db, "b", stage="approved")
    event(db, "b", "gha-7-3", "approved", "ready")
    ok, why = chain.again(db, "work", since=50, owner="gha-7-")
    assert ok and "1 left" in why


def test_work_stops_when_nothing_left(db):
    paper(db, "b", stage="approved")
    event(db, "b", "gha-7-3", "approved", "ready")
    assert chain.again(db, "work", since=50, owner="gha-7-") == (False, "work: nothing left")


def test_no_progress_breaks_the_loop(db):
    paper(db, "a")
    event(db, "a", "gha-7-1", "discovered", "ready")       # a failure, backing off
    event(db, "z", "gha-6-1", "approved", "ready")         # another run's progress
    event(db, "y", "gha-7-1", "approved", "ready", at=10)  # before this run started
    ok, why = chain.again(db, "work", since=50, owner="gha-7-")
    assert not ok and "moved none" in why


def test_dead_and_review_papers_are_not_work_left(db):
    paper(db, "a", status="dead")
    paper(db, "b", stage="staged", status="review")
    assert chain.left_and_moved(db, "work", 0, "gha-1-")[0] == 0


def test_publish_counts_done_papers_of_its_owner(db):
    paper(db, "a", stage="approved")
    event(db, "b", "publish", "published", "done")
    assert chain.again(db, "publish", since=50, owner="publish")[0]
    assert not chain.again(db, "publish", since=150, owner="publish")[0]


def test_route_waiting_rows_are_left_but_not_progress(db):
    paper(db, "a", stage="approved", report="r1")
    paper(db, "b", stage="approved", report="r2")
    route_job.ensure_table(db)
    db.execute("INSERT INTO routing VALUES ('a', 'waiting', '{}', 0, 100)")
    assert chain.left_and_moved(db, "route", 50, "route") == (2, 0)
    db.execute("INSERT INTO routing VALUES ('b', 'done', '{}', 0, 100)")
    assert chain.again(db, "route", since=50, owner="route") == (True, "route: 1 left, this run moved 1")


def test_unknown_job_is_an_error(db):
    with pytest.raises(ValueError):
        chain.again(db, "deploy", 0, "x")
