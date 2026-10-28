import pytest

from pwpipe import state
from pwpipe.db import SqliteDb
from pwpipe.keys import KeyPool, QuotaExhausted


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def pool(n=3, per_day=100, shared=None):
    env = {"GEMINI_API_KEY": "k1"}
    env.update({f"GEMINI_API_KEY_{i}": f"k{i}" for i in range(2, n + 1)})
    env["GEMINI_API_KEY_9"] = ""          # an empty secret is ignored
    p = KeyPool.from_env("gemini", per_day, env=env, shared=shared)
    p.clock = Clock()
    return p


def test_reads_numbered_keys_and_skips_blanks():
    assert [k.value for k in pool(3).keys] == ["k1", "k2", "k3"]


def test_rotates_to_spread_use():
    p = pool(3)
    used = [p.acquire() for _ in range(6)]
    assert sorted(used) == ["k1", "k1", "k2", "k2", "k3", "k3"]


def test_429_parks_a_key_then_it_returns():
    p = pool(2)
    p.report("k1", 429, retry_after=30)
    assert {p.acquire() for _ in range(3)} == {"k2"}
    p.clock.t += 31
    assert "k1" in {p.acquire() for _ in range(4)}


def test_bad_key_is_retired_others_continue():
    p = pool(2)
    p.report("k2", 401)
    assert {p.acquire() for _ in range(5)} == {"k1"}


def test_daily_limit_then_exhausted():
    p = pool(2, per_day=2)
    for _ in range(4):
        p.acquire()
    with pytest.raises(QuotaExhausted):
        p.acquire()


def test_daily_count_is_shared_between_workers(tmp_path):
    db = SqliteDb(str(tmp_path / "s.db"))
    state.migrate(db)
    a, b = pool(1, per_day=3, shared=db), pool(1, per_day=3, shared=db)
    a.acquire(), b.acquire(), a.acquire()
    with pytest.raises(QuotaExhausted):
        b.acquire()                        # the key's 3 uses today are spent
    assert "k1" not in str(db.execute("SELECT key FROM meta"))   # only a hash is stored


def test_provider_overload_rests_key_briefly_not_retired():
    p = pool(1)
    p.report("k1", 503)
    with pytest.raises(QuotaExhausted):
        p.acquire()
    p.clock.t += 31
    assert p.acquire() == "k1" and not p.keys[0].retired
