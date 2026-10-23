"""Shared configuration. All secrets from environment, never logged."""
from __future__ import annotations

import base64
import os


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN")
TELEGRAM_WEBHOOK_SECRET = _get("TELEGRAM_WEBHOOK_SECRET")
ALLOWED_TELEGRAM_USER_ID = _get("ALLOWED_TELEGRAM_USER_ID")
PUBLIC_BASE_URL = _get("PUBLIC_BASE_URL")

GEMINI_API_KEY = _get("GEMINI_API_KEY") or _get("GOOGLE_API_KEY")
GEMINI_PRIMARY = _get("GEMINI_PRIMARY", "gemini-2.5-flash")
GEMINI_FALLBACK = _get("GEMINI_FALLBACK", "gemini-3-flash-preview")

# Drive: OAuth preferred (user decision), service account kept as fallback.
GOOGLE_OAUTH_CLIENT_ID = _get("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET = _get("GOOGLE_OAUTH_CLIENT_SECRET")
GOOGLE_OAUTH_REFRESH_TOKEN = _get("GOOGLE_OAUTH_REFRESH_TOKEN")
GOOGLE_SERVICE_ACCOUNT_JSON_B64 = _get("GOOGLE_SERVICE_ACCOUNT_JSON")

COMPOSIO_API_KEY = _get("COMPOSIO_API_KEY")
COMPOSIO_ENTITY_ID = _get("COMPOSIO_ENTITY_ID", "default")
LINKEDIN_AUTHOR = _get("LINKEDIN_AUTHOR")  # organization URN for company page

SUPABASE_URL = _get("SUPABASE_URL").rstrip("/")
SUPABASE_KEY = _get("SUPABASE_KEY") or _get("SUPABASE_SERVICE_KEY")

DRAFT_TTL_HOURS = int(_get("DRAFT_TTL_HOURS", "24") or "24")
MAX_VIDEO_MB = int(_get("MAX_VIDEO_MB", "512") or "512")
MEDIA_RESOLUTION = _get("MEDIA_RESOLUTION", "low")
AUDIO_ONLY_DEFAULT = _get("AUDIO_ONLY_DEFAULT", "true").lower() in ("1", "true", "yes")

SUPPORTED_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}

TELEGRAM_API = "https://api.telegram.org"


def service_account_info():
    """Decode GOOGLE_SERVICE_ACCOUNT_JSON (base64) if present, else None."""
    if not GOOGLE_SERVICE_ACCOUNT_JSON_B64:
        return None
    try:
        raw = base64.b64decode(GOOGLE_SERVICE_ACCOUNT_JSON_B64).decode("utf-8")
        import json

        return json.loads(raw)
    except Exception:
        return None
