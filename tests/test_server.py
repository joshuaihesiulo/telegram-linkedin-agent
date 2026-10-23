"""Webhook: secret gate, user gate, invalid-link guidance, approve idempotency."""
from datetime import datetime, timedelta, timezone

import app.server as server
from app.schemas import DraftRecord, VideoAnalysis
from app.store import MemoryStore
from fastapi.testclient import TestClient


def _analysis():
    return VideoAnalysis(transcript="t", key_points=["a", "b", "c"],
                         main_takeaway="m", tone="t")


def _client(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # rebind config values (imported by value in modules)
    import importlib

    import app.config as cfg

    importlib.reload(cfg)
    importlib.reload(server)
    return TestClient(server.app)


def test_health():
    from fastapi.testclient import TestClient as TC

    c = TC(server.app)
    assert c.get("/health").json() == {"ok": True}


def test_other_users_ignored(monkeypatch):
    sent = []
    monkeypatch.setattr(server.telegram, "send_message",
                        lambda *a, **k: sent.append(a) or {})
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "999")
    import importlib

    import app.config as cfg

    importlib.reload(cfg)
    importlib.reload(server)
    from fastapi.testclient import TestClient as TC

    c = TC(server.app)
    r = c.post("/webhook", json={
        "update_id": 9001,
        "message": {"message_id": 1, "chat": {"id": 111},
                    "from": {"id": 111}, "text": "hello"}})
    assert r.json().get("ignored") is True
    assert sent == []


def test_invalid_link_guidance(monkeypatch):
    sent = []
    monkeypatch.setattr(server.telegram, "send_message",
                        lambda *a, **k: sent.append((a, k)) or {})
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_ID", raising=False)
    import importlib

    import app.config as cfg

    importlib.reload(cfg)
    importlib.reload(server)
    monkeypatch.setattr(server.telegram, "send_message",
                        lambda *a, **k: sent.append((a, k)) or {})
    from fastapi.testclient import TestClient as TC

    c = TC(server.app)
    c.post("/webhook", json={
        "update_id": 9002,
        "message": {"message_id": 1, "chat": {"id": 5},
                    "from": {"id": 5},
                    "text": "https://example.com/not-drive"}})
    assert sent and "doesn't look like" in sent[-1][0][1]


def test_double_approve_publishes_once(monkeypatch):
    calls = []
    monkeypatch.setattr(server.telegram, "send_message",
                        lambda *a, **k: {})
    monkeypatch.setattr(server.telegram, "answer_callback",
                        lambda *a, **k: None)
    monkeypatch.setattr(server.drive, "download_file",
                        lambda fid, dest: dest)
    monkeypatch.setattr(server, "publish_video_post",
                        lambda text, path: calls.append(1) or "https://linkedin/x")
    # seed a pending draft in the real store singleton
    now = datetime.now(timezone.utc)
    d = DraftRecord(draft_id="d_double", telegram_id=5, drive_file_id="fid",
                    analysis_json=_analysis(), draft_text="post",
                    status="pending", created_at=now,
                    expires_at=now + timedelta(hours=24))
    server.store.save_draft(d)
    server.store.set_state(server.store.get_state(5).model_copy(
        update={"telegram_id": 5, "state": "AWAITING_APPROVAL"}))
    from fastapi.testclient import TestClient as TC

    c = TC(server.app)
    body = lambda uid: {"update_id": uid, "callback_query": {
        "id": "cb", "from": {"id": 5},
        "message": {"chat": {"id": 5}}, "data": "approve:d_double"}}
    c.post("/webhook", json=body(9101))
    c.post("/webhook", json=body(9102))  # double-tap, different update_id
    assert len(calls) == 1
    assert server.store.get_draft("d_double").status == "published"
