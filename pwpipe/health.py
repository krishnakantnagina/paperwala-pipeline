"""Daily health check: is the pipeline able to run, and is it running?

Run by .github/workflows/health.yml. Prints a report; exits non-zero when
something needs a person, and the workflow then opens a GitHub issue (which
emails the repo owner). Nothing here changes any paper.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import state
from .db import Db

STALL_HOURS = 8           # scheduled every 6 h: no worker heartbeat for 8 h = stalled
DRIVE_FULL = 0.85         # warn when the Drive is this full
DEAD_ALERT = 25           # dead papers worth a look


@dataclass
class Health:
    problems: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems


def check_state(db: Db, health: Health, now: float | None = None) -> None:
    now = now or time.time()
    rows = state.summary(db)
    by = {}
    for r in rows:
        by[(r["stage"], r["status"])] = by.get((r["stage"], r["status"]), 0) + r["n"]
    total = sum(by.values())
    waiting = by.get(("discovered", "ready"), 0) + by.get(("discovered", "leased"), 0)
    dead = sum(n for (stage, status), n in by.items() if status == "dead")
    review = sum(n for (stage, status), n in by.items() if status == "review")
    approved = by.get(("approved", "ready"), 0)
    published = sum(n for (stage, _), n in by.items() if stage in ("published", "live_verified"))
    health.facts.update(total=total, waiting=waiting, review=review, approved=approved,
                        published=published, dead=dead)

    beat = db.execute("SELECT value FROM meta WHERE key = 'heartbeat:worker'")
    last = int(beat[0]["value"]) if beat else None
    health.facts["last_worker_heartbeat_hours"] = round((now - last) / 3600, 1) if last else None
    if waiting and (last is None or now - last > STALL_HOURS * 3600):
        health.problems.append(
            f"{waiting} papers waiting but no worker has run for over {STALL_HOURS} h "
            "(schedule disabled, Actions minutes, or every run failing)")
    if dead >= DEAD_ALERT:
        health.problems.append(f"{dead} papers are dead (failed 5 times); see their last_error")


def check_drive(drive, health: Health) -> None:
    try:
        about = drive.about()
    except Exception as exc:  # the login itself is what is being tested
        health.problems.append(f"Google Drive login failed: {exc}. Re-run pwpipe.drive_auth "
                               "and update the GDRIVE_OAUTH_JSON secret.")
        return
    quota = about.get("storageQuota", {})
    used, limit = int(quota.get("usage", 0)), int(quota.get("limit", 0) or 0)
    health.facts["drive_account"] = about.get("user", {}).get("emailAddress")
    health.facts["drive_used_gb"] = round(used / 1e9, 2)
    if limit:
        health.facts["drive_limit_gb"] = round(limit / 1e9, 2)
        if used / limit >= DRIVE_FULL:
            health.problems.append(f"Google Drive is {used / limit:.0%} full")


def report(health: Health) -> str:
    lines = ["## Pipeline health", ""]
    lines += [f"- **{k}**: {v}" for k, v in health.facts.items()]
    lines += ["", "### Problems" if health.problems else "No problems."]
    lines += [f"- {p}" for p in health.problems]
    return "\n".join(lines)
