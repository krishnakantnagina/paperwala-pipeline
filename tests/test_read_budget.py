"""Row-read budget. On 2026-10-28 the pipeline used 700M Turso row reads in one
day (limit 500M a month), which blocked every database in the org, the live
site's included. These tests fail if claim() or the chain counts go back to
reading the whole papers table."""

import pytest

from pwpipe import chain, state
from pwpipe.db import SqliteDb

N = 3000


@pytest.fixture
def big(tmp_path):
    db = SqliteDb(str(tmp_path / "s.db"))
    state.migrate(db)
    db.batch([("INSERT INTO papers (drive_id, catalog, stage, status, updated_at) "
               "VALUES (:d, 'c', 'discovered', 'ready', :u)", {"d": f"d{i}", "u": i})
              for i in range(N)])
    return db


def traced(db, fn):
    seen = []
    db.conn.set_trace_callback(seen.append)
    try:
        fn()
    finally:
        db.conn.set_trace_callback(None)
    return seen


def plans(db, statements):
    out = []
    for sql in statements:
        if sql.lstrip().upper().startswith(("SELECT", "UPDATE", "INSERT")):
            out += [r["detail"] for r in db.execute("EXPLAIN QUERY PLAN " + sql)]
    return out


def test_claim_does_not_sort_or_scan_every_ready_paper(big):
    seen = traced(big, lambda: state.claim(big, "discovered", "w", 5))
    detail = " | ".join(plans(big, seen))
    assert "TEMP B-TREE" not in detail
    assert "ix_papers_claim" in detail
    assert "SCAN papers" not in detail.replace("SCAN papers USING", "")


def test_claim_still_hands_out_each_paper_once_in_priority_order(big):
    big.execute("UPDATE papers SET priority = 5 WHERE drive_id = 'd2999'")
    first = state.claim(big, "discovered", "a", 3)
    assert "d2999" in {p["drive_id"] for p in first}      # the priority one is taken first
    second = state.claim(big, "discovered", "b", 3)
    assert not {p["drive_id"] for p in first} & {p["drive_id"] for p in second}
    assert len(first) == len(second) == 3


def test_chain_counts_stop_at_the_cap(big, monkeypatch):
    monkeypatch.setattr(chain, "CAP", 10)
    left, moved = chain.left_and_moved(big, "work", 0, "x")
    assert left == 10 and moved == 0
    assert chain.again(big, "work", 0, "x")[0] is False


def test_sync_and_route_find_site_rows_without_scanning_per_paper(tmp_path):
    from pwpipe import publish, route_job, sync

    class Counting(SqliteDb):
        scans = 0

        def execute(self, sql, params=None):
            if "FROM pyq_papers" in sql and "WHERE p.id" not in sql and "WHERE id" not in sql:
                Counting.scans += 1
            return super().execute(sql, params)

    live = Counting(str(tmp_path / "live.db"))
    live.execute("CREATE TABLE subjects (id INTEGER PRIMARY KEY, university TEXT, program TEXT, "
                 "branch TEXT, semester INT, code TEXT, name TEXT, aliases TEXT)")
    live.execute("CREATE TABLE pyq_papers (id INTEGER PRIMARY KEY, subject_id INT, title TEXT, "
                 "year TEXT, exam_type TEXT, drive_link TEXT, preview_link TEXT)")
    live.execute("INSERT INTO subjects (id, university, program, name) VALUES (1, 'RGPV', 'B.Tech', 'X')")
    for i in range(30):
        live.execute("INSERT INTO pyq_papers (subject_id, title, drive_link, preview_link) "
                     "VALUES (1, 't', :v, :p)",
                     {"v": publish.view_link(f"f{i}"), "p": publish.preview_link(f"f{i}")})
    index = publish.LinkIndex(live)
    for i in range(30):
        assert sync._site_row(live, f"f{i}", index)["university"] == "RGPV"
        assert route_job._site(live, {"drive_id": f"f{i}"}, index)[1]["name"] == "X"
    assert sync._site_row(live, "nope", index) is None
    assert Counting.scans == 1                      # the index, once - not 61 instr() scans
