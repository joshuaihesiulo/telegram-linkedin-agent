"""Store: dedupe, expiry, idempotent status transitions."""
from datetime import datetime, timedelta, timezone

from app.schemas import DraftRecord, UserState, VideoAnalysis
from app.store import MemoryStore


def _draft(store, status="pending", expired=False):
    now = datetime.now(timezone.utc)
    d = DraftRecord(
        draft_id="d_test123",
        telegram_id=111,
        drive_file_id="fid",
        analysis_json=VideoAnalysis(
            transcript="t", key_points=["a", "b", "c"],
            main_takeaway="m", tone="t"),
        draft_text="hello",
        status=status,
        created_at=now - timedelta(hours=1),
        expires_at=now - timedelta(seconds=1) if expired
        else now + timedelta(hours=24),
    )
    store.save_draft(d)
    return d


def test_seen_update_dedupes():
    s = MemoryStore()
    assert s.seen_update(42) is False
    assert s.seen_update(42) is True


def test_expired_draft_read_as_expired():
    s = MemoryStore()
    _draft(s, expired=True)
    d = s.get_draft("d_test123")
    assert d is not None and d.status == "expired"


def test_latest_pending_none_when_expired():
    s = MemoryStore()
    _draft(s, expired=True)
    assert s.latest_pending(111) is None


def test_state_roundtrip():
    s = MemoryStore()
    s.set_state(UserState(telegram_id=7, state="AWAITING_LINK",
                          instructions="bold"))
    assert s.get_state(7).instructions == "bold"
