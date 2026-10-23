"""Composio Platform adapter: per-Telegram-user sessions and tool execution.

One stable user id per Telegram user (`tg_<telegram_id>`) keeps each person's
connections isolated. Secrets are never logged.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import config

log = logging.getLogger("composio")
_sessions: dict[str, object] = {}


def _client():
    if not config.COMPOSIO_API_KEY:
        raise RuntimeError(
            "COMPOSIO_API_KEY is not configured. Add it to .env to connect "
            "social accounts.")
    from composio import Composio  # imported lazily so local dev works

    return Composio(api_key=config.COMPOSIO_API_KEY)


def _user_key(user_id: int) -> str:
    return f"tg_{user_id}"


def _session(user_id: int, toolkit: str):
    key = f"{_user_key(user_id)}:{toolkit.lower()}"
    if key in _sessions:
        return _sessions[key]
    client = _client()
    session = client.sessions.create(
        user_id=_user_key(user_id),
        toolkits=[toolkit.lower()],
    )
    _sessions[key] = session
    return session


def _value(obj, *names, default=""):
    for name in names:
        if isinstance(obj, dict) and obj.get(name):
            return obj[name]
        v = getattr(obj, name, None)
        if v:
            return v
    return default


def start_connection(user_id: int, toolkit: str) -> str:
    """Create a hosted Connect Link. Returns the URL for Telegram."""
    session = _session(user_id, toolkit)
    authorize = getattr(session, "authorize", None)
    if not callable(authorize):
        raise RuntimeError(
            "Installed Composio SDK has no session.authorize(). "
            "Upgrade with: pip install -U 'composio>=0.30.0'")

    request = authorize(toolkit.lower())
    url = _value(request, "redirect_url", "redirectUrl", "url")
    if not url:
        raise RuntimeError("Composio returned no Connect Link")
    return str(url)


def is_connected(user_id: int, toolkit: str) -> bool:
    """True when this user has an ACTIVE connected account for the toolkit."""
    wanted = toolkit.lower()
    try:
        session = _session(user_id, toolkit)
        result = session.toolkits(is_connected=True)
        items = _value(result, "items", default=result)
        for item in items or []:
            slug = str(_value(item, "slug", "toolkit_slug", default="")).lower()
            if slug and slug != wanted:
                continue
            conn = _value(item, "connection", "connected_account", default=None)
            if conn is None:
                return True
            active = _value(conn, "is_active", "isActive", default=None)
            if active is True:
                return True
            status = str(_value(conn, "status", default="")).upper()
            if status in ("ACTIVE", "CONNECTED"):
                return True
    except Exception as e:  # noqa: BLE001
        log.warning("connection check failed for %s: %s", toolkit, e)

    # Fallback: direct connected-accounts list.
    try:
        client = _client()
        result = client.connected_accounts.list(
            user_ids=[_user_key(user_id)], statuses=["ACTIVE"])
        items = _value(result, "items", default=result)
        for item in items or []:
            tk = _value(item, "toolkit", default=None)
            slug = str(_value(tk, "slug", default="")).lower()
            if slug == wanted:
                return True
    except Exception as e:  # noqa: BLE001
        log.warning("connected account list failed: %s", e)
    return False


def execute(user_id: int, toolkit: str, action: str, arguments: dict | None = None):
    """Run one tool inside the user's session. Raises on SDK failure."""
    session = _session(user_id, toolkit)
    try:
        return session.execute(action, arguments=arguments or {})
    except TypeError:
        # Older SDK signature: execute(slug, arguments)
        return session.execute(action, arguments or {})


def execute_data(user_id: int, toolkit: str, action: str,
                 arguments: dict | None = None):
    result = execute(user_id, toolkit, action, arguments)
    return getattr(result, "data", result)


def upload_file(toolkit: str, action: str, path) -> dict:
    """Stage a local file in Composio's file store for one tool action.

    Tool schemas that accept files expect an object shaped
    ``{name, mimetype, s3key}``. ``session.execute`` does not auto-upload
    local paths, so callers stage the file here first and pass the returned
    descriptor as the argument value. Uploads stream in chunks, so large
    videos never become base64 strings in memory.
    """
    p = Path(path)
    if not p.is_file():
        raise RuntimeError(f"Media file not found: {p}")
    client = _client()
    try:
        from composio.core.models._files import FileUploadable  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "Installed Composio SDK cannot stage files. "
            "Install with: pip install 'composio==0.30.0'") from e
    uploadable = FileUploadable.from_path(
        client=client.client,
        file=p,
        tool=action,
        toolkit=toolkit.upper(),
    )
    return uploadable.model_dump()
