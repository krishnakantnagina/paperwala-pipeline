"""The read fuse: Turso reports rows_read per statement; a run counts them and
stops at PIPELINE_READ_BUDGET instead of using up the org's monthly quota."""

import io
import json

import pytest

from pwpipe import db as dbmod
from pwpipe.db import DbError, ReadBudgetExceeded, ReadMeter, TursoDb


def reply(*steps_read, kind="execute"):
    if kind == "execute":
        res = {"type": "ok", "response": {"type": "execute", "result": {
            "cols": [], "rows": [], "rows_read": steps_read[0], "rows_written": 0}}}
    else:
        res = {"type": "ok", "response": {"type": "batch", "result": {
            "step_results": [{"cols": [], "rows": [], "rows_read": n, "rows_written": 1}
                             for n in steps_read], "step_errors": [None] * len(steps_read)}}}
    return json.dumps({"results": [res, {"type": "ok", "response": {"type": "close"}}]}).encode()


class FakeResp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def turso(monkeypatch):
    meter = ReadMeter(budget=1000)
    monkeypatch.setattr(dbmod, "METER", meter)
    replies = []
    monkeypatch.setattr(dbmod.urllib.request, "urlopen",
                        lambda req, timeout=0: FakeResp(replies.pop(0)))
    return TursoDb("https://x.turso.io", "t"), meter, replies


def test_rows_read_are_counted_for_execute_and_batch(turso):
    db, meter, replies = turso
    replies += [reply(40), reply(10, 25, kind="batch")]
    db.execute("select 1")
    db.batch([("update a set b = 1", None), ("update c set d = 2", None)])
    assert (meter.read, meter.written, meter.statements) == (75, 2, 2)


def test_run_stops_when_the_budget_is_reached(turso):
    db, meter, replies = turso
    replies += [reply(999), reply(5)]
    db.execute("select 1")                 # 999 < 1000: still allowed
    db.execute("select 1")                 # reaches 1004
    with pytest.raises(ReadBudgetExceeded) as err:
        db.execute("select 1")             # refused before it is sent
    assert "PIPELINE_READ_BUDGET" in str(err.value)
    assert len(replies) == 0               # the third request never went out


def test_budget_error_is_a_dberror_so_jobs_treat_it_as_infrastructure():
    from pwpipe.worker import _infra
    assert issubclass(ReadBudgetExceeded, DbError)
    assert _infra(ReadBudgetExceeded("x"))


def test_budget_zero_turns_the_fuse_off():
    m = ReadMeter(budget=0)
    m.add(10**9, 0)
    m.check()


def test_budget_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("PIPELINE_READ_BUDGET", "123")
    assert ReadMeter().budget == 123
    monkeypatch.delenv("PIPELINE_READ_BUDGET")
    assert ReadMeter().budget == ReadMeter.DEFAULT


def test_summary_line_reports_the_cost():
    m = ReadMeter(budget=500)
    m.add(42, 3, 2)
    assert "42 rows read" in m.summary() and "500" in m.summary()
