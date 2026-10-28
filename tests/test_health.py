from pwpipe import state
from pwpipe.db import SqliteDb
from pwpipe.health import Health, check_drive, check_state


def make_db(tmp_path):
    db = SqliteDb(str(tmp_path / "s.db"))
    state.migrate(db)
    for i in range(3):
        db.execute("INSERT INTO papers (drive_id, catalog, updated_at) VALUES (:d, 'c', 1)",
                   {"d": f"p{i}"})
    return db


def test_stalled_pipeline_is_a_problem(tmp_path):
    db = make_db(tmp_path)
    h = Health()
    check_state(db, h, now=10_000_000)
    assert not h.ok and "no worker has run" in h.problems[0]


def test_recent_heartbeat_is_healthy(tmp_path):
    db = make_db(tmp_path)
    db.execute("INSERT INTO meta VALUES ('heartbeat:worker', '9999000')")
    h = Health()
    check_state(db, h, now=10_000_000)
    assert h.ok and h.facts["waiting"] == 3


def test_drive_login_failure_is_a_problem():
    class Broken:
        def about(self):
            raise RuntimeError("invalid_grant")
    h = Health()
    check_drive(Broken(), h)
    assert "Drive login failed" in h.problems[0]


def test_full_drive_is_a_problem():
    class Full:
        def about(self):
            return {"user": {"emailAddress": "x"}, "storageQuota": {"usage": "90", "limit": "100"}}
    h = Health()
    check_drive(Full(), h)
    assert "90% full" in h.problems[0]
