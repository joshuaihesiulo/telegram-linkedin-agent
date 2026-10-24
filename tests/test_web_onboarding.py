"""Web onboarding wizard: token gate, steps, shared state, Telegram handoff."""
import app.config as config
import app.onboarding as onboarding
import app.platforms as platforms
import app.server as server
import app.telegram as telegram
import app.web_tokens as web_tokens
from app.store import store
from fastapi.testclient import TestClient


class Background:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))

    def run(self):
        while self.tasks:
            fn, args, kwargs = self.tasks.pop(0)
            fn(*args, **kwargs)


def _capture_telegram(monkeypatch):
    sent = []

    def fake_send(chat_id, text, reply_markup=None, **kwargs):
        sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup})
        return {}

    monkeypatch.setattr(telegram, "send_message", fake_send)
    return sent


def _client(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_WEBHOOK_SECRET", "test-onboard-secret")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://bot.example")
    return TestClient(server.app)


def test_tokens_roundtrip_and_reject(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
    tok = web_tokens.make_token(4242)
    assert web_tokens.verify(tok) == 4242

    head, exp, sig = tok.split(".")
    assert web_tokens.verify(tok + "x") is None
    assert web_tokens.verify(f"{head}.{exp}.{'0' * len(sig)}") is None
    assert web_tokens.verify(f"{head}.{int(exp) - 40 * 24 * 3600}.{sig}") is None
    assert web_tokens.verify(web_tokens.make_token(4242, ttl_seconds=-5)) is None
    assert web_tokens.verify("not-a-token") is None
    assert web_tokens.verify("") is None


def test_invalid_link_gets_400(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/onboard?t=bogus")
    assert r.status_code == 400
    assert "invalid or has expired" in r.text
    r = c.post("/onboard/platform", data={"t": "bogus", "platform": "linkedin"})
    assert r.status_code == 400


def test_web_full_onboarding_flow(monkeypatch):
    sent = _capture_telegram(monkeypatch)
    c = _client(monkeypatch)
    monkeypatch.setattr(platforms, "is_connected", lambda uid, slug: False)
    monkeypatch.setattr(platforms, "connect_url",
                        lambda uid, slug: "https://connect.example/ln_1")
    monkeypatch.setattr(onboarding.gemini, "analyze_style",
                        lambda samples: "Warm, direct, short paragraphs.")
    uid = 7201
    t = web_tokens.make_token(uid)

    # Step 1: platform
    r = c.get(f"/onboard?t={t}")
    assert r.status_code == 200 and "Where do you want to post" in r.text
    r = c.post("/onboard/platform", data={"t": t, "platform": "linkedin"},
               follow_redirects=False)
    assert r.status_code == 303
    s = store.get_state(uid)
    assert s.platform == "linkedin" and s.onboarding_step == "connect"

    # Step 2: connect (hosted link rendered, then check advances)
    r = c.get(f"/onboard?t={t}")
    assert "https://connect.example/ln_1" in r.text
    monkeypatch.setattr(platforms, "is_connected", lambda uid, slug: True)
    monkeypatch.setattr(platforms, "detect_accounts",
                        lambda uid, slug: [platforms.Account(
                            "urn:li:person:42", "Ada Lovelace")])
    r = c.post("/onboard/connect/check", data={"t": t},
               follow_redirects=False)
    assert r.status_code == 303
    s = store.get_state(uid)
    assert s.account == "urn:li:person:42"
    assert s.onboarding_step == "samples"

    # Step 3: voice samples (short set is rejected)
    r = c.post("/onboard/samples", data={"t": t, "samples": ["a", "b", "c"]})
    assert "I found 3 posts" in r.text
    assert store.get_state(uid).onboarding_step == "samples"

    posts = [f"past post {i}" for i in range(10)]
    r = c.post("/onboard/samples", data={"t": t, "samples": posts},
               follow_redirects=False)
    assert r.status_code == 303
    s = store.get_state(uid)          # TestClient runs background tasks
    assert s.onboarding_step == "mode"
    assert "Warm, direct" in s.voice_guide
    assert any("Voice learned" in m["text"] for m in sent)

    # Step 4: mode -> schedule
    r = c.post("/onboard/mode", data={"t": t, "mode": "scheduled"},
               follow_redirects=False)
    assert r.status_code == 303
    assert store.get_state(uid).onboarding_step == "schedule"

    # Step 5: schedule
    r = c.post("/onboard/schedule", data={
        "t": t, "freq": "weekdays", "time": "18:00",
        "timezone": "Africa/Lagos"}, follow_redirects=False)
    assert r.status_code == 303
    s = store.get_state(uid)
    assert s.schedule == "weekdays 18:00" and s.timezone == "Africa/Lagos"
    assert s.onboarding_step == "media"

    # Step 6: media -> live; Telegram gets the handoff message
    r = c.post("/onboard/media", data={"t": t, "media": "image"},
               follow_redirects=False)
    assert r.status_code == 303
    s = store.get_state(uid)
    assert s.onboarded is True
    assert s.state == "AWAITING_MATERIAL"
    assert s.media_pref == "image"
    assert any("You are all set" in m["text"] for m in sent)

    r = c.get(f"/onboard?t={t}")
    assert "You are all set" in r.text


def test_schedule_validation_renders_errors(monkeypatch):
    _capture_telegram(monkeypatch)
    c = _client(monkeypatch)
    uid = 7202
    t = web_tokens.make_token(uid)
    onboarding._save(uid, state="ONBOARDING", onboarding_step="schedule",
                     onboarded=False, platform="linkedin",
                     account="urn:li:person:9", voice_guide="x",
                     posting_mode="scheduled")

    r = c.post("/onboard/schedule", data={
        "t": t, "freq": "daily", "time": "25:00", "timezone": "UTC"})
    assert "Use a 24-hour time" in r.text
    r = c.post("/onboard/schedule", data={
        "t": t, "freq": "daily", "time": "09:00", "timezone": "Mars/Base"})
    assert "Unknown timezone" in r.text
    r = c.post("/onboard/schedule", data={
        "t": t, "freq": "custom", "time": "09:00", "timezone": "UTC"})
    assert "Pick at least one day" in r.text

    r = c.post("/onboard/schedule", data={
        "t": t, "freq": "custom", "time": "09:00", "timezone": "UTC",
        "days": ["mon", "wed"]}, follow_redirects=False)
    assert r.status_code == 303
    s = store.get_state(uid)
    assert s.schedule == "mon,wed 09:00" and s.onboarding_step == "media"


def test_first_contact_invite_links_web_then_chat(monkeypatch):
    sent = _capture_telegram(monkeypatch)
    _client(monkeypatch)
    uid = 7203
    server.handle_message(uid, uid, {"text": "hello"}, Background())
    s = store.get_state(uid)
    assert s.invite_sent is True
    assert s.state == "IDLE"                    # not started yet
    kb = sent[-1]["markup"]["inline_keyboard"]
    urls = [b.get("url") or "" for row in kb for b in row]
    assert any("/onboard?t=" in u for u in urls)
    tok = next(u for u in urls if u).split("t=", 1)[1]
    assert web_tokens.verify(tok) == uid        # link is bound to this user

    # A second plain message starts the in-chat flow instead.
    server.handle_message(uid, uid, {"text": "hi again"}, Background())
    s = store.get_state(uid)
    assert s.state == "ONBOARDING" and s.onboarding_step == "platform"


def test_start_with_web_keeps_progress_and_resumes_in_chat(monkeypatch):
    sent = _capture_telegram(monkeypatch)
    _client(monkeypatch)
    uid = 7204
    onboarding._save(uid, state="ONBOARDING", onboarding_step="samples",
                     onboarded=False, platform="linkedin", samples=["a"])
    server.handle_message(uid, uid, {"text": "/start"}, Background())

    s = store.get_state(uid)
    assert s.samples == ["a"] and s.onboarding_step == "samples"
    assert "step 3 of 6" in sent[-1]["text"]
    kb = sent[-1]["markup"]["inline_keyboard"]
    assert any("/onboard?t=" in (b.get("url") or "")
               for row in kb for b in row)
    assert any(b.get("callback_data") == "ob:chat"
               for row in kb for b in row)

    onboarding.handle_callback(uid, uid, "ob:chat", Background())
    assert "1/10" in sent[-1]["text"]           # resumed the samples step
    assert store.get_state(uid).samples == ["a"]


def test_account_picker_and_analyzing_page(monkeypatch):
    _capture_telegram(monkeypatch)
    c = _client(monkeypatch)
    monkeypatch.setattr(platforms, "is_connected", lambda uid, slug: True)
    monkeypatch.setattr(platforms, "detect_accounts", lambda uid, slug: [
        platforms.Account("urn:li:person:1", "Ada"),
        platforms.Account("urn:li:organization:2", "Acme")])
    monkeypatch.setattr(onboarding.gemini, "analyze_style",
                        lambda samples: "G.")
    uid = 7205
    t = web_tokens.make_token(uid)
    onboarding._save(uid, state="ONBOARDING", onboarding_step="connect",
                     onboarded=False, platform="linkedin")

    r = c.post("/onboard/connect/check", data={"t": t},
               follow_redirects=False)
    assert r.status_code == 303
    assert store.get_state(uid).onboarding_step == "account"
    r = c.get(f"/onboard?t={t}")
    assert "Ada" in r.text and "Acme" in r.text

    r = c.post("/onboard/account", data={
        "t": t, "account": "urn:li:organization:2"},
        follow_redirects=False)
    assert r.status_code == 303
    s = store.get_state(uid)
    assert s.account == "urn:li:organization:2"
    assert s.onboarding_step == "samples"

    # The analyzing page starts the background analysis in the same request.
    onboarding._save(uid, samples=["x"] * 10, onboarding_step="analyzing")
    r = c.get(f"/onboard?t={t}")
    assert "Learning your voice" in r.text
    s = store.get_state(uid)
    assert s.onboarding_step == "mode" and s.voice_guide == "G."
