"""Publish tests: a local SQLite stands in for the live site database."""

import json

import pytest

from pwpipe import publish, state
from pwpipe.db import SqliteDb


class FakeDrive:
    def __init__(self):
        self.shared = []

    def share_anyone(self, file_id):
        self.shared.append(file_id)


@pytest.fixture
def dbs(tmp_path):
    st = SqliteDb(str(tmp_path / "state.db"))
    state.migrate(st)
    live = SqliteDb(str(tmp_path / "live.db"))
    live.execute("CREATE TABLE pyq_papers (id INTEGER PRIMARY KEY, drive_link TEXT, "
                 "preview_link TEXT)")
    return st, live


def approved(st, live, orig, new, on_site=True):
    st.execute("""INSERT INTO papers (drive_id, catalog, stage, status, output_file_id,
                  updated_at) VALUES (:d, 'c', 'approved', 'ready', :n, 1)""",
               {"d": orig, "n": new})
    if on_site:
        live.execute("INSERT INTO pyq_papers (drive_link, preview_link) VALUES (:v, :p)",
                     {"v": publish.view_link(orig), "p": publish.preview_link(orig)})


def go(st, live, drive=None, public=True, site=True, limit=50):
    return publish.run(st, live, drive or FakeDrive(), "https://site", limit, 30, "t",
                       log=lambda *_: None, check_public=lambda f: public,
                       check_site=lambda u: site)


def test_publish_switches_link_and_records_old_one(dbs):
    st, live = dbs
    approved(st, live, "orig1", "new1")
    drive = FakeDrive()
    stats = go(st, live, drive)
    assert stats.published == 1 and stats.rows_switched == 1
    row = live.execute("SELECT * FROM pyq_papers")[0]
    assert row["drive_link"] == publish.view_link("new1")
    assert row["preview_link"] == publish.preview_link("new1")
    p = state.get(st, "orig1")
    assert (p["stage"], p["status"]) == ("published", "done")
    assert json.loads(p["published_rows"])[0]["drive_link"] == publish.view_link("orig1")
    assert drive.shared == ["new1"]


def test_paper_not_on_site_is_parked_not_failed(dbs):
    st, live = dbs
    approved(st, live, "orig1", "new1", on_site=False)
    stats = go(st, live)
    assert stats.not_on_site == 1
    assert (state.get(st, "orig1")["stage"], state.get(st, "orig1")["status"]) == ("approved", "done")


def test_copy_that_will_not_open_publicly_is_not_switched(dbs):
    st, live = dbs
    approved(st, live, "orig1", "new1")
    stats = go(st, live, public=False)
    assert stats.failed == 1
    assert live.execute("SELECT drive_link FROM pyq_papers")[0]["drive_link"] == \
        publish.view_link("orig1")


def test_site_down_publishes_nothing(dbs):
    st, live = dbs
    approved(st, live, "orig1", "new1")
    with pytest.raises(publish.PublishStop):
        go(st, live, site=False)
    assert live.execute("SELECT drive_link FROM pyq_papers")[0]["drive_link"] == \
        publish.view_link("orig1")


def test_crash_after_switch_is_recorded_next_run(dbs):
    st, live = dbs
    approved(st, live, "orig1", "new1")
    live.execute("UPDATE pyq_papers SET drive_link = :v, preview_link = :p",
                 {"v": publish.view_link("new1"), "p": publish.preview_link("new1")})
    stats = go(st, live)
    assert stats.published == 1 and state.get(st, "orig1")["stage"] == "published"


def test_unpublish_restores_original_and_sends_to_review(dbs):
    st, live = dbs
    approved(st, live, "orig1", "new1")
    go(st, live)
    assert publish.unpublish(st, live, ["orig1"], "t", "bad erase") == 1
    row = live.execute("SELECT * FROM pyq_papers")[0]
    assert row["drive_link"] == publish.view_link("orig1")
    p = state.get(st, "orig1")
    assert (p["stage"], p["status"]) == ("staged", "review")


def test_limit_and_batches(dbs):
    st, live = dbs
    for i in range(45):
        approved(st, live, f"o{i}", f"n{i}")
    stats = go(st, live, limit=30)
    assert stats.published == 30
