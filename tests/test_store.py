"""Store: dedupe, expiry, idempotent status transitions."""
from datetime import datetime, timedelta, timezone

from app.schemas import DraftRecord, UserState, VideoAnalysis
from app.store import MemoryStore


def _draft(store, status="pending", expired=False, draft_id="d_test123"):
    now = datetime.now(timezone.utc)
    d = DraftRecord(
        draft_id=draft_id,
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


def test_profile_fields_roundtrip_through_supabase_shape():
    from app.store import _profile, _state_from_row

    s = UserState(
        telegram_id=9, state="AWAITING_MATERIAL", onboarded=True,
        platform="linkedin", account="urn:li:person:1",
        samples=["one", "two"], voice_guide="Direct.",
        posting_mode="scheduled", schedule="daily 09:00",
        timezone="UTC", media_pref="image",
    )
    row = {"telegram_id": 9, "state": s.state, "instructions": "",
           "drive_file_id": "", "profile": _profile(s)}
    back = _state_from_row(row)
    assert back.onboarded is True
    assert back.samples == ["one", "two"]
    assert back.schedule == "daily 09:00"
    assert back.media_pref == "image"


def test_due_scheduled_only_returns_due():
    s = MemoryStore()
    now = datetime.now(timezone.utc)
    due = _draft(s, status="scheduled", draft_id="d_due")
    due.scheduled_for = now - timedelta(minutes=1)
    later = _draft(s, status="scheduled", draft_id="d_later")
    later.scheduled_for = now + timedelta(hours=1)
    s.save_draft(later)
    out = s.due_scheduled(now)
    assert [d.draft_id for d in out] == [due.draft_id]
