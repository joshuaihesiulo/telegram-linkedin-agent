"""Onboarding state machine: platform, connect, samples, mode, media."""
import app.onboarding as onboarding
import app.platforms as platforms
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


def _capture(monkeypatch):
    sent = []

    def fake_send(chat_id, text, reply_markup=None, **kwargs):
        sent.append({"chat_id": chat_id, "text": text,
                     "markup": reply_markup})
        return {}

    monkeypatch.setattr(onboarding.telegram, "send_message", fake_send)
    return sent


def _connected(monkeypatch):
    monkeypatch.setattr(onboarding.platforms, "is_connected",
                        lambda user_id, slug: True)
    monkeypatch.setattr(
        onboarding.platforms, "detect_accounts",
        lambda user_id, slug: [platforms.Account("urn:li:person:42", "Ada")])


def test_full_onboarding_with_samples_and_schedule(monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(onboarding.platforms, "is_connected",
                        lambda user_id, slug: False)
    monkeypatch.setattr(onboarding.platforms, "connect_url",
                        lambda user_id, slug: "https://connect.example/ln_1")
    uid = 7001

    onboarding.start(uid, uid)
    s = store.get_state(uid)
    assert s.state == "ONBOARDING" and s.onboarding_step == "platform"
    assert sent[-1]["markup"]["inline_keyboard"]
    assert "Where do you want to post" in sent[-1]["text"]

    bg = Background()
    onboarding.handle_callback(uid, uid, "ob:platform:linkedin", bg)
    bg.run()
    s = store.get_state(uid)
    assert s.platform == "linkedin" and s.onboarding_step == "connect"
    assert any((m["markup"] or {}).get("inline_keyboard") for m in sent)

    _connected(monkeypatch)
    bg = Background()
    onboarding.handle_callback(uid, uid, "ob:connected", bg)
    bg.run()
    s = store.get_state(uid)
    assert s.account == "urn:li:person:42"
    assert s.onboarding_step == "samples"

    for i in range(9):
        onboarding.handle_text(uid, uid, f"past post {i}", bg)
    assert len(store.get_state(uid).samples) == 9

    onboarding.handle_callback(uid, uid, "ob:samples_done", bg)
    assert store.get_state(uid).onboarding_step == "samples"
    assert "at least" in sent[-1]["text"].lower()

    onboarding.handle_text(uid, uid, "past post 9", bg)
    assert len(store.get_state(uid).samples) == 10

    monkeypatch.setattr(onboarding.gemini, "analyze_style",
                        lambda samples: "Warm, direct, short paragraphs.")
    onboarding.handle_callback(uid, uid, "ob:samples_done", bg)
    bg.run()
    s = store.get_state(uid)
    assert s.onboarding_step == "mode"
    assert "Warm, direct" in s.voice_guide

    bg = Background()
    onboarding.handle_callback(uid, uid, "ob:mode:scheduled", bg)
    assert store.get_state(uid).onboarding_step == "schedule"

    bg = Background()
    onboarding.handle_text(uid, uid, "weekdays 18:00 Africa/Lagos", bg)
    s = store.get_state(uid)
    assert s.onboarding_step == "media"
    assert s.schedule == "weekdays 18:00"
    assert s.timezone == "Africa/Lagos"

    bg = Background()
    onboarding.handle_callback(uid, uid, "ob:media:image", bg)
    s = store.get_state(uid)
    assert s.onboarded is True
    assert s.state == "AWAITING_MATERIAL"
    assert s.media_pref == "image"
    assert s.posting_mode == "scheduled"


def test_on_demand_completes(monkeypatch):
    _capture(monkeypatch)
    _connected(monkeypatch)
    uid = 7002
    onboarding._save(
        uid, state="ONBOARDING", onboarding_step="mode", onboarded=False,
        platform="linkedin", account="urn:li:person:42",
        samples=["a", "b"],
        voice_guide="Direct.", posting_mode="on_demand",
    )
    bg = Background()
    onboarding.handle_callback(uid, uid, "ob:mode:on_demand", bg)
    assert store.get_state(uid).onboarding_step == "media"

    onboarding.handle_callback(uid, uid, "ob:media:text", bg)
    s = store.get_state(uid)
    assert s.onboarded and s.posting_mode == "on_demand"
    assert s.schedule == ""


def test_account_validation(monkeypatch):
    sent = _capture(monkeypatch)
    uid = 7003
    onboarding._save(uid, state="ONBOARDING", onboarding_step="account",
                     onboarded=False, platform="linkedin")
    onboarding.handle_text(uid, uid, "not-a-urn", Background())
    assert "does not look like" in sent[-1]["text"]
    assert store.get_state(uid).onboarding_step == "account"

    onboarding.handle_text(uid, uid, "urn:li:person:99", Background())
    s = store.get_state(uid)
    assert s.account == "urn:li:person:99"
    assert s.onboarding_step == "samples"


def test_first_time_any_message_starts_onboarding(monkeypatch):
    sent = _capture(monkeypatch)
    import app.server as server

    uid = 7004
    bg = Background()
    server.handle_message(uid, uid, {"text": "hello"}, bg)
    s = store.get_state(uid)
    assert s.state == "ONBOARDING" and s.onboarding_step == "platform"
    assert "Where do you want to post" in sent[-1]["text"]


def test_connect_keyboard_without_url_has_no_url_button():
    from app import telegram

    kb = telegram.connect_keyboard("")
    buttons = [b for row in kb["inline_keyboard"] for b in row]
    assert all("url" not in b for b in buttons)
    assert any(b["callback_data"] == "ob:connected" for b in buttons)


def test_start_mid_onboarding_resumes_without_clearing(monkeypatch):
    sent = _capture(monkeypatch)
    import app.server as server

    uid = 7005
    onboarding._save(uid, state="ONBOARDING", onboarding_step="samples",
                     onboarded=False, platform="linkedin",
                     account="urn:li:person:7", samples=["a", "b", "c"])
    server.handle_message(uid, uid, {"text": "/start"}, Background())

    s = store.get_state(uid)
    assert s.samples == ["a", "b", "c"]      # progress kept
    assert s.onboarding_step == "samples"
    assert "3/10" in sent[-1]["text"]        # re-prompted the current step


def test_settings_mid_onboarding_resets(monkeypatch):
    sent = _capture(monkeypatch)
    import app.server as server

    uid = 7006
    onboarding._save(uid, state="ONBOARDING", onboarding_step="samples",
                     onboarded=False, platform="linkedin", samples=["a"])
    server.handle_message(uid, uid, {"text": "/settings"}, Background())

    s = store.get_state(uid)
    assert s.samples == []
    assert s.onboarding_step == "platform"
    assert "six steps" in sent[-1]["text"]
