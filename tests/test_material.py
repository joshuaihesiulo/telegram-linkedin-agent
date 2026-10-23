"""Material drafting, media intake, approval, and scheduling callbacks."""
from datetime import datetime, timedelta, timezone

import app.pipeline as pipeline
import app.server as server
from app.schemas import DraftPost, DraftRecord, UserState
from app.store import store


class Background:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))

    def run(self):
        while self.tasks:
            fn, args, kwargs = self.tasks.pop(0)
            fn(*args, **kwargs)


def _post():
    return DraftPost(hook="Hook", body="Body", cta="CTA",
                     hashtags=["#one", "#two", "#three"])


def _user(uid, **updates):
    base = dict(
        telegram_id=uid, state="AWAITING_MATERIAL", onboarded=True,
        platform="linkedin", account="urn:li:person:42",
        account_label="Ada", voice_guide="Direct.", samples=["a", "b"],
        posting_mode="on_demand",
    )
    base.update(updates)
    store.set_state(UserState(**base))
    return store.get_state(uid)


def test_material_pipeline_creates_platform_draft(monkeypatch):
    uid = 8101
    _user(uid)
    sent = []
    monkeypatch.setattr(pipeline.telegram, "send_message",
                        lambda *a, **k: sent.append(a) or {})
    monkeypatch.setattr(pipeline.gemini, "write_material_post",
                        lambda *a, **k: _post())

    pipeline.run_material_pipeline(uid, "We shipped a new feature today.")

    d = store.latest_pending(uid)
    assert d is not None
    assert d.platform == "linkedin"
    assert d.media_type == "text"
    assert d.status == "pending"
    assert "Hook" in d.draft_text
    assert store.get_state(uid).state == "AWAITING_APPROVAL"
    assert any("Platform: LinkedIn" in a[1] for a in sent)


def test_approve_publishes_platform_draft(monkeypatch):
    uid = 8102
    _user(uid)
    now = datetime.now(timezone.utc)
    d = DraftRecord(
        draft_id="d_approve1", telegram_id=uid, draft_text="post",
        status="pending", created_at=now, expires_at=now + timedelta(hours=2),
        platform="linkedin", media_type="text",
    )
    store.save_draft(d)
    store.set_state(store.get_state(uid).model_copy(
        update={"state": "AWAITING_APPROVAL"}))
    sent = []
    monkeypatch.setattr(server.telegram, "send_message",
                        lambda *a, **k: sent.append(a) or {})
    monkeypatch.setattr(server.publisher, "publish_draft",
                        lambda draft: "https://linkedin.example/post/9")

    server.handle_callback(uid, uid, f"approve:{d.draft_id}", Background())

    d = store.get_draft(d.draft_id)
    assert d.status == "published"
    assert d.post_url == "https://linkedin.example/post/9"
    assert store.get_state(uid).state == "AWAITING_MATERIAL"
    assert sent and "Published" in sent[-1][1]


def test_schedule_callback_queues_next_slot(monkeypatch):
    uid = 8103
    _user(uid, posting_mode="scheduled", schedule="daily 09:00",
          timezone="UTC")
    now = datetime.now(timezone.utc)
    d = DraftRecord(
        draft_id="d_sched2", telegram_id=uid, draft_text="post",
        status="pending", created_at=now, expires_at=now + timedelta(hours=2),
        platform="linkedin", media_type="text",
    )
    store.save_draft(d)
    store.set_state(store.get_state(uid).model_copy(
        update={"state": "AWAITING_APPROVAL"}))
    sent = []
    monkeypatch.setattr(server.telegram, "send_message",
                        lambda *a, **k: sent.append(a) or {})

    server.handle_callback(uid, uid, f"sched:{d.draft_id}", Background())

    d = store.get_draft(d.draft_id)
    assert d.status == "scheduled"
    assert d.scheduled_for is not None and d.scheduled_for > now
    assert store.get_state(uid).state == "AWAITING_MATERIAL"
    assert sent and "Scheduled for" in sent[-1][1]


def test_photo_with_caption_starts_material(monkeypatch):
    uid = 8104
    _user(uid, platform="facebook", account="123456789")
    captured = []
    monkeypatch.setattr(server, "run_material_pipeline",
                        lambda *a: captured.append(a))
    bg = Background()
    msg = {
        "message_id": 1, "chat": {"id": uid}, "from": {"id": uid},
        "photo": [{"file_id": "photo1", "file_size": 1000}],
        "caption": "Launch day",
    }
    server.handle_message(uid, uid, msg, bg)
    bg.run()

    assert captured
    args = captured[0]
    assert args[1] == "Launch day"
    assert args[2] == "photo1"
    assert args[3] == "image"


def test_oversize_video_is_refused(monkeypatch):
    uid = 8105
    _user(uid)
    sent = []
    monkeypatch.setattr(server.telegram, "send_message",
                        lambda *a, **k: sent.append(a) or {})
    captured = []
    monkeypatch.setattr(server, "run_material_pipeline",
                        lambda *a: captured.append(a))
    msg = {
        "message_id": 1, "chat": {"id": uid}, "from": {"id": uid},
        "video": {"file_id": "vid1", "file_size": 25 * 1024 * 1024},
        "caption": "Big video",
    }
    server.handle_message(uid, uid, msg, Background())

    assert not captured
    assert sent and "20 MB" in sent[-1][1]


def test_instagram_uploaded_image_hands_off_to_public_url(monkeypatch):
    uid = 8106
    _user(uid, platform="instagram", account="178414")
    sent = []
    monkeypatch.setattr(server.telegram, "send_message",
                        lambda *a, **k: sent.append(a) or {})
    captured = []
    monkeypatch.setattr(
        server, "run_material_pipeline",
        lambda *a, **k: captured.append((a, k)))
    photo = {"message_id": 1, "chat": {"id": uid}, "from": {"id": uid},
             "photo": [{"file_id": "photo9", "file_size": 900}]}

    # 1. Photo with no caption: remembered, then asked for a public URL.
    server.handle_message(uid, uid, photo, Background())
    assert store.get_state(uid).pending_media_file_id == "photo9"

    # 2. Public URL replaces the unusable Telegram file.
    server.handle_message(uid, uid, {**photo, "photo": None,
                                     "text": "https://cdn.example/i.jpg"},
                          Background())
    s = store.get_state(uid)
    assert s.pending_media_file_id == ""
    assert s.pending_media_url == "https://cdn.example/i.jpg"

    # 3. Caption arrives: pipeline gets the URL as media for Instagram.
    bg = Background()
    server.handle_message(uid, uid, {**photo, "photo": None,
                                     "text": "Launch day"}, bg)
    bg.run()
    assert captured
    args, kwargs = captured[0]
    # run_material_pipeline(chat_id, material, media_file_id, media_type, media_url)
    assert args[1] == "Launch day"
    assert args[2] == ""
    assert args[3] == "image"
    assert args[4] == "https://cdn.example/i.jpg"
