"""Scheduling: parse human schedules and publish due drafts.

No external scheduler dependency. A daemon thread wakes every
SCHEDULER_INTERVAL_SEC and publishes drafts whose scheduled_for is due.
"""
from __future__ import annotations

import logging
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config, telegram
from .store import store

log = logging.getLogger("scheduler")

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_ALIASES = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3,
    "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_thread: threading.Thread | None = None
_started = False


@dataclass(frozen=True)
class Schedule:
    text: str
    time: str
    days: frozenset[int]
    timezone: str


def parse_schedule(text: str, default_tz: str = "UTC") -> Schedule:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Send a schedule, for example: daily 09:00 Africa/Lagos")

    tokens = raw.replace(",", " ").split()
    tz_name = default_tz or "UTC"
    for i, tok in enumerate(tokens):
        if "/" in tok or tok.upper() == "UTC":
            try:
                ZoneInfo(tok)
            except ZoneInfoNotFoundError:
                raise ValueError(f"Unknown timezone: {tok}") from None
            tz_name = tok
            tokens.pop(i)
            break

    import re

    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", " ".join(tokens))
    if not m:
        raise ValueError("I could not find a time. Use HH:MM (24-hour).")
    hh, mm = int(m.group(1)), int(m.group(2))
    time_str = f"{hh:02d}:{mm:02d}"
    rest = (raw.lower().replace(m.group(0), " ")
            .replace(tz_name.lower(), " ")
            .replace(",", " "))
    words = [w for w in rest.split() if w]

    if any(w in ("daily", "everyday") for w in words) or "every" in words:
        days = frozenset(range(7))
    elif "weekdays" in words:
        days = frozenset(range(5))
    elif "weekends" in words:
        days = frozenset({5, 6})
    else:
        picked = {_DAY_ALIASES[w] for w in words if w in _DAY_ALIASES}
        if not picked:
            raise ValueError(
                "Say daily, weekdays, weekends, or list days "
                "(e.g. mon,wed,fri).")
        days = frozenset(picked)

    if days == frozenset(range(7)):
        label = "daily"
    elif days == frozenset(range(5)):
        label = "weekdays"
    elif days == frozenset({5, 6}):
        label = "weekends"
    else:
        label = ",".join(_DAYS[d] for d in range(7) if d in days)
    return Schedule(f"{label} {time_str}", time_str, days, tz_name)


def next_slot(schedule_text: str, tz_name: str = "UTC",
              after: datetime | None = None) -> datetime:
    sched = parse_schedule(schedule_text, default_tz=tz_name)
    tz = ZoneInfo(sched.timezone)
    now = (after or datetime.now(timezone.utc)).astimezone(tz)
    hh, mm = (int(x) for x in sched.time.split(":"))
    for offset in range(0, 8):
        day = now.date() + timedelta(days=offset)
        if day.weekday() not in sched.days:
            continue
        candidate = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
        if candidate > now:
            return candidate.astimezone(timezone.utc)
    raise ValueError("No matching schedule slot in the next 7 days")


def run_due(now: datetime | None = None) -> int:
    """Publish every due scheduled draft. Returns the number processed."""
    from .publisher import publish_draft

    due = store.due_scheduled(now)
    processed = 0
    for draft in due:
        draft.status = "publishing"
        store.save_draft(draft)
        try:
            url = publish_draft(draft)
            draft.status = "published"
            draft.post_url = url
            store.save_draft(draft)
            telegram.send_message(
                draft.telegram_id,
                f"Scheduled post published{f': {url}' if url else '.'}")
            processed += 1
        except Exception as e:  # noqa: BLE001
            log.exception("scheduled publish failed for %s", draft.draft_id)
            draft.status = "failed"
            store.save_draft(draft)
            telegram.send_message(
                draft.telegram_id,
                f"Scheduled post failed: {e}\nSend /new and try again.")
    return processed


def _loop() -> None:
    while True:
        try:
            run_due()
        except Exception:  # noqa: BLE001
            log.exception("scheduler tick failed")
        _time.sleep(max(5, config.SCHEDULER_INTERVAL_SEC))


def start_scheduler() -> None:
    global _thread, _started
    if _started or not config.SCHEDULER_ENABLED:
        return
    _started = True
    _thread = threading.Thread(target=_loop, name="post-scheduler", daemon=True)
    _thread.start()
    log.info("scheduler started (interval=%ss)", config.SCHEDULER_INTERVAL_SEC)
