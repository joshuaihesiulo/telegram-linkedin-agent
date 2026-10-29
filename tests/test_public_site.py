"""Public website entry: landing -> Telegram link -> personal wizard handoff."""
import app.config as config
import app.onboarding as onboarding
import app.pairing as pairing
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


def _capture(monkeypatch):
    sent = []

    def fake_send(chat_id, text, reply_markup=None, **kwargs):
        sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup})
        return {}

    monkeypatch.setattr(telegram, "send_message", fake_send)
    return sent


def _client(monkeypatch):
    pairing.reset()
    monkeypatch.setattr(config, "TELEGRAM_WEBHOOK_SECRET", "test-site-secret")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://bot.example")
    monkeypatch.setattr(config, "TELEGRAM_BOT_USERNAME", "testbot")
    return TestClient(server.app)


def _sid(resp) -> str:
    return resp.headers["location"].split("s=", 1)[1]


def test_public_home_lists_platforms(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/")
    assert r.status_code == 200
    assert "action='/begin'" in r.text
    for name in ("LinkedIn", "X (Twitter)", "Facebook Page", "Instagram"):
        assert name in r.text
    assert "https://t.me/testbot" in r.text


def test_begin_shows_telegram_deep_link(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/begin", data={"platform": "linkedin"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/link?s=")
    sid = _sid(r)
    assert pairing.get(sid)["platform"] == "linkedin"

    r = c.get(f"/link?s={sid}", follow_redirects=False)
    assert r.status_code == 303                     # waiting page lives at /status
    r = c.get(r.headers["location"])
    assert r.status_code == 200
    assert f"https://t.me/testbot?start={sid}" in r.text
    assert "http-equiv='refresh'" in r.text


def test_begin_rejects_unknown_platform(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/begin", data={"platform": "myspace"})
    assert r.status_code == 400
    assert "Pick one of the platforms" in r.text


def test_telegram_claim_then_browser_handoff(monkeypatch):
    sent = _capture(monkeypatch)
    c = _client(monkeypatch)
    uid = 8201
    sid = _sid(c.post("/begin", data={"platform": "facebook"},
                      follow_redirects=False))

    server.handle_message(uid, uid, {"text": f"/start {sid}"}, Background())

    s = store.get_state(uid)
    assert s.platform == "facebook" and s.onboarding_step == "connect"
    assert "Telegram linked" in sent[-1]["text"]
    kb = sent[-1]["markup"]["inline_keyboard"]
    urls = [b.get("url") or "" for row in kb for b in row]
    tok = next(u for u in urls if "/onboard?t=" in u).split("t=", 1)[1]
    assert web_tokens.verify(tok) == uid

    r = c.get(f"/link/status?s={sid}", follow_redirects=False)
    assert r.status_code == 303
    dest = r.headers["location"]
    assert dest.startswith("/onboard?t=")
    assert web_tokens.verify(dest.split("t=", 1)[1]) == uid


def test_claim_keeps_existing_progress(monkeypatch):
    _capture(monkeypatch)
    c = _client(monkeypatch)
    uid = 8202
    onboarding._save(uid, state="ONBOARDING", onboarding_step="samples",
                     platform="twitter", samples=["a", "b"])
    sid = _sid(c.post("/begin", data={"platform": "linkedin"},
                      follow_redirects=False))

    server.handle_message(uid, uid, {"text": f"/start {sid}"}, Background())

    s = store.get_state(uid)
    assert s.platform == "twitter"          # existing choice wins
    assert s.onboarding_step == "samples"
    assert s.samples == ["a", "b"]


def test_claim_when_already_onboarded_shows_dashboard(monkeypatch):
    sent = _capture(monkeypatch)
    c = _client(monkeypatch)
    uid = 8203
    onboarding._save(uid, onboarded=True, state="AWAITING_MATERIAL",
                     platform="linkedin", account="urn:li:person:1",
                     voice_guide="x", media_pref="text")
    sid = _sid(c.post("/begin", data={"platform": "linkedin"},
                      follow_redirects=False))

    server.handle_message(uid, uid, {"text": f"/start {sid}"}, Background())

    assert "You are set up" in sent[-1]["text"]
    assert store.get_state(uid).platform == "linkedin"   # not reset


def test_expired_or_unknown_sessions(monkeypatch):
    sent = _capture(monkeypatch)
    c = _client(monkeypatch)

    r = c.get("/link/status?s=bogus")
    assert r.status_code == 400 and "expired" in r.text

    server.handle_message(8204, 8204, {"text": "/start bogus"}, Background())
    assert "invalid or has expired" in sent[-1]["text"]

    sid = pairing.create("linkedin")
    pairing._sessions[sid]["expires"] = 0        # force TTL expiry
    r = c.get(f"/link/status?s={sid}")
    assert r.status_code == 400
    assert pairing.get(sid) is None


def test_link_needs_bot_username(monkeypatch):
    c = _client(monkeypatch)
    monkeypatch.setattr(config, "TELEGRAM_BOT_USERNAME", "")
    monkeypatch.setattr(telegram, "bot_username", lambda: "")
    sid = _sid(c.post("/begin", data={"platform": "linkedin"},
                      follow_redirects=False))
    r = c.get(f"/link/status?s={sid}")
    assert r.status_code == 500
    assert "TELEGRAM_BOT_USERNAME" in r.text


def test_pairing_first_claim_wins():
    pairing.reset()
    sid = pairing.create("linkedin")
    assert pairing.claim(sid, 1)["telegram_id"] == 1
    assert pairing.claim(sid, 1)["telegram_id"] == 1     # idempotent
    assert pairing.claim(sid, 2) is None                 # not transferable
    assert pairing.telegram_id(sid) == 1
    pairing.reset()
