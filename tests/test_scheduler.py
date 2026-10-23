"""Schedule parsing, next-slot math, and due-draft publication."""
from datetime import datetime, timedelta, timezone

import app.publisher as publisher
import app.scheduler as scheduler
from app.schemas import DraftRecord
from app.store import MemoryStore


def _draft(store, status="pending", scheduled_for=None):
    now = datetime.now(timezone.utc)
    d = DraftRecord(
        draft_id="d_sched1",
        telegram_id=99,
        draft_text="hello",
        status=status,
        created_at=now - timedelta(minutes=10),
        expires_at=now + timedelta(days=2),
        platform="linkedin",
        scheduled_for=scheduled_for,
    )
    store.save_draft(d)
    return d


def test_parse_daily_with_timezone():
    s = scheduler.parse_schedule("daily 09:00 Africa/Lagos")
    assert s.text == "daily 09:00"
    assert s.timezone == "Africa/Lagos"
    assert len(s.days) == 7


def test_parse_weekdays_uses_default_timezone():
    s = scheduler.parse_schedule("weekdays 18:00", default_tz="Europe/London")
    assert s.text == "weekdays 18:00"
    assert s.timezone == "Europe/London"
    assert s.days == frozenset(range(5))


def test_parse_explicit_days():
    s = scheduler.parse_schedule("mon,wed,fri 08:30 America/New_York")
    assert s.days == frozenset({0, 2, 4})
    assert s.text == "mon,wed,fri 08:30"


def test_parse_rejects_unknown_timezone():
    try:
        scheduler.parse_schedule("daily 09:00 Mars/Olympus")
    except ValueError as e:
        assert "Unknown timezone" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_next_slot_rolls_to_tomorrow():
    after = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    assert scheduler.next_slot("daily 09:00", "UTC", after) == datetime(
        2026, 1, 2, 9, 0, tzinfo=timezone.utc)


def test_run_due_publishes_once(monkeypatch):
    s = MemoryStore()
    monkeypatch.setattr(scheduler, "store", s)
    sent = []
    monkeypatch.setattr(scheduler.telegram, "send_message",
                        lambda *a, **k: sent.append(a) or {})
    monkeypatch.setattr(publisher, "publish_draft",
                        lambda d: "https://linkedin.example/post/1")
    now = datetime.now(timezone.utc)
    d = _draft(s, status="scheduled", scheduled_for=now - timedelta(minutes=1))

    assert scheduler.run_due(now) == 1
    assert s.get_draft(d.draft_id).status == "published"
    assert s.get_draft(d.draft_id).post_url == "https://linkedin.example/post/1"
    assert sent and "published" in sent[-1][1].lower()

    # Second pass must not republish.
    assert scheduler.run_due(now) == 0


def test_due_scheduled_ignores_future(monkeypatch):
    s = MemoryStore()
    monkeypatch.setattr(scheduler, "store", s)
    now = datetime.now(timezone.utc)
    _draft(s, status="scheduled", scheduled_for=now + timedelta(hours=1))
    assert scheduler.run_due(now) == 0
