"""A pool of API keys per provider, rotated so no single key hits its limit.

Keys come from the environment (GitHub secrets in CI, ``~/.paperwala`` on a
laptop): ``GEMINI_API_KEY``, ``GEMINI_API_KEY_2`` ... ``GEMINI_API_KEY_20``,
and the same for ``GROQ``. Adding a key is adding a secret - no code change.

- Each call takes the key with the most room left today.
- A 429 parks that key for a while (``Retry-After`` when the provider says).
- A 401/403 retires the key for the rest of the run; the others carry on.
- When every key is out, ``QuotaExhausted`` is raised: callers leave the
  paper for a later run instead of failing it. Running out of free quota is
  never the paper's fault.
- Daily counts can be shared through the state database, so 16 parallel
  workers together respect a key's daily limit, not 16 times it.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field

MAX_KEYS = 20
DEFAULT_COOLDOWN = 60


class QuotaExhausted(RuntimeError):
    pass


@dataclass
class _Key:
    value: str
    used_today: int = 0
    parked_until: float = 0.0
    retired: bool = False

    @property
    def tag(self) -> str:
        """Safe to log and store: never the key itself."""
        return hashlib.sha256(self.value.encode()).hexdigest()[:10]


@dataclass
class KeyPool:
    provider: str
    keys: list[_Key]
    per_day: int
    clock: object = field(default=time.time, repr=False)
    shared: object = field(default=None, repr=False)   # db for cross-worker daily counts

    @classmethod
    def from_env(cls, provider: str, per_day: int, env=None, shared=None) -> "KeyPool":
        env = os.environ if env is None else env
        name = f"{provider.upper()}_API_KEY"
        values = [env.get(name, "")] + [env.get(f"{name}_{i}", "") for i in range(2, MAX_KEYS + 1)]
        seen, keys = set(), []
        for v in (v.strip() for v in values):
            if v and v not in seen:
                seen.add(v)
                keys.append(_Key(v))
        return cls(provider, keys, per_day, shared=shared)

    def __len__(self) -> int:
        return len(self.keys)

    def _day(self) -> str:
        return time.strftime("%Y%m%d", time.gmtime(self.clock()))

    def _shared_count(self, key: _Key, add: int) -> int:
        """Today's uses of this key across all workers (state DB meta row)."""
        if self.shared is None:
            key.used_today += add
            return key.used_today
        name = f"quota:{self.provider}:{key.tag}:{self._day()}"
        rows = self.shared.execute(
            "INSERT INTO meta (key, value) VALUES (:k, :v) ON CONFLICT (key) DO UPDATE "
            "SET value = CAST(CAST(value AS INTEGER) + :v AS TEXT) RETURNING value",
            {"k": name, "v": str(add)})
        key.used_today = int(rows[0]["value"])
        return key.used_today

    def acquire(self) -> str:
        now = self.clock()
        live = [k for k in self.keys if not k.retired and k.parked_until <= now
                and k.used_today < self.per_day]
        if not live:
            raise QuotaExhausted(f"{self.provider}: all {len(self.keys)} keys parked, "
                                 f"retired or at {self.per_day}/day")
        key = min(live, key=lambda k: k.used_today)
        if self._shared_count(key, 1) > self.per_day:
            key.parked_until = now + 3600
            return self.acquire()
        return key.value

    def report(self, value: str, status: int, retry_after: float | None = None) -> None:
        """Tell the pool how a call with this key went (HTTP status)."""
        key = next((k for k in self.keys if k.value == value), None)
        if key is None or status < 400:
            return
        if status in (401, 403):
            key.retired = True
        elif status == 429:
            key.parked_until = self.clock() + (retry_after or DEFAULT_COOLDOWN)

    def status(self) -> list[dict]:
        return [{"key": k.tag, "used_today": k.used_today, "retired": k.retired,
                 "parked": k.parked_until > self.clock()} for k in self.keys]
