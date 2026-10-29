"""Short-lived Telegram linking sessions for the public website.

A visitor picks a platform on the public site; we create a random session id
and show `t.me/<bot>?start=<sid>`. The bot claims the session, which tells the
browser which Telegram user to hand off to. Sessions store only the chosen
platform and expire after TTL_SECONDS (they live in memory, like the default
state store; a server restart simply makes an unclaimed link expire).
"""
from __future__ import annotations

import secrets
import threading
import time

TTL_SECONDS = 30 * 60

_sessions: dict[str, dict] = {}
_lock = threading.Lock()


def _alive(s: dict | None) -> bool:
    return bool(s) and s["expires"] >= time.time()


def create(platform: str) -> str:
    """Start a linking session for a platform slug; returns the public id."""
    now = time.time()
    with _lock:
        for key in [k for k, v in _sessions.items() if v["expires"] < now]:
            del _sessions[key]
        sid = secrets.token_urlsafe(16)
        _sessions[sid] = {"platform": platform, "telegram_id": None,
                          "expires": now + TTL_SECONDS}
    return sid


def get(sid: str) -> dict | None:
    with _lock:
        s = _sessions.get(sid)
        if not _alive(s):
            _sessions.pop(sid, None)
            return None
        return dict(s)


def claim(sid: str, telegram_id: int) -> dict | None:
    """Bind a Telegram user to a site session. First claim wins."""
    with _lock:
        s = _sessions.get(sid)
        if not _alive(s):
            _sessions.pop(sid, None)
            return None
        if s["telegram_id"] not in (None, int(telegram_id)):
            return None
        s["telegram_id"] = int(telegram_id)
        return dict(s)


def telegram_id(sid: str) -> int | None:
    s = get(sid)
    return s["telegram_id"] if s else None


def reset() -> None:
    """Test helper: forget every session."""
    with _lock:
        _sessions.clear()
