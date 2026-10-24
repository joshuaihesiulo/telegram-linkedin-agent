"""Signed personal setup links: /start -> {PUBLIC_BASE_URL}/onboard?t=<token>.

The token binds a browser session to exactly one Telegram user id, so the web
wizard can write the same onboarding state the chat flow uses. HMAC-SHA256
with a server secret; no database lookups, and links are re-sent on /start.
"""
from __future__ import annotations

import hashlib
import hmac
import time

from . import config

# Long-lived: the bot regenerates the link on every /start anyway.
TTL_SECONDS = 30 * 24 * 3600


def _secret() -> bytes:
    raw = (config.TELEGRAM_WEBHOOK_SECRET or config.TELEGRAM_BOT_TOKEN
           or "local-dev-onboard")
    return hashlib.sha256(b"onboard:" + raw.encode()).digest()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_token(user_id: int, ttl_seconds: int = TTL_SECONDS) -> str:
    exp = int(time.time()) + int(ttl_seconds)
    payload = f"{int(user_id)}.{exp}"
    return f"{payload}.{_sign(payload)}"


def verify(token: str | None) -> int | None:
    """Return the telegram_id for a valid, unexpired token, else None."""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    uid_s, exp_s, sig = parts
    if not (uid_s.isdigit() and exp_s.isdigit()):
        return None
    if int(exp_s) < int(time.time()):
        return None
    if not hmac.compare_digest(sig, _sign(f"{uid_s}.{exp_s}")):
        return None
    return int(uid_s)


def web_enabled() -> bool:
    return bool(config.PUBLIC_BASE_URL)


def web_url(user_id: int) -> str:
    """Personal onboarding URL, or "" when no public base URL is configured."""
    if not web_enabled():
        return ""
    return (f"{config.PUBLIC_BASE_URL.rstrip('/')}"
            f"/onboard?t={make_token(user_id)}")
