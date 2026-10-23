"""Google Drive: file-ID parsing + streamed download.

Auth: user OAuth (preferred) with service-account fallback.
Never scrape the share link (avoids the large-file virus-scan page) —
always Drive API v3 files.get / alt=media by file ID.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import httpx

from . import config

_DRIVE_ID_PATTERNS = [
    re.compile(r"/file/d/([A-Za-z0-9_-]+)"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]+)"),
    re.compile(r"^([A-Za-z0-9_-]{10,})$"),
]


def parse_drive_file_id(url_or_id: str) -> str | None:
    s = (url_or_id or "").strip()
    for pat in _DRIVE_ID_PATTERNS:
        m = pat.search(s)
        if m:
            return m.group(1)
    return None


def _oauth_access_token() -> str:
    """Exchange refresh token for a short-lived access token."""
    if not (config.GOOGLE_OAUTH_CLIENT_ID and config.GOOGLE_OAUTH_CLIENT_SECRET
            and config.GOOGLE_OAUTH_REFRESH_TOKEN):
        raise RuntimeError("Google OAuth not configured")
    r = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": config.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": config.GOOGLE_OAUTH_CLIENT_SECRET,
            "refresh_token": config.GOOGLE_OAUTH_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _service_account_token() -> str:
    from google.oauth2 import service_account

    info = config.service_account_info()
    if not info:
        raise RuntimeError("No service account configured")
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    from google.auth.transport.requests import Request

    creds.refresh(Request())
    return creds.token


def access_token() -> str:
    try:
        return _oauth_access_token()
    except Exception:
        return _service_account_token()


def check_access(file_id: str) -> tuple[bool, str, int]:
    """Metadata-only check. Returns (ok, name_or_error, size_bytes)."""
    token = access_token()
    r = httpx.get(
        f"https://www.googleapis.com/drive/v3/files/{file_id}",
        params={"fields": "id,name,mimeType,size", "supportsAllDrives": "true"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    if r.status_code in (403, 404):
        return False, (
            "I can't open that file. Check sharing / OAuth access, "
            "then send the link again."
        ), 0
    r.raise_for_status()
    meta = r.json()
    return True, meta.get("name", file_id), int(meta.get("size", 0) or 0)


def download_file(file_id: str, dest: Path) -> Path:
    """Stream download to dest. Enforces MAX_VIDEO_MB."""
    token = access_token()
    max_bytes = config.MAX_VIDEO_MB * 1024 * 1024
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream(
        "GET",
        f"https://www.googleapis.com/drive/v3/files/{file_id}",
        params={"alt": "media", "supportsAllDrives": "true"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    ) as r:
        r.raise_for_status()
        size = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(1024 * 256):
                size += len(chunk)
                if size > max_bytes:
                    f.close()
                    try:
                        os.remove(dest)
                    except OSError:
                        pass
                    raise ValueError(
                        f"Video exceeds MAX_VIDEO_MB={config.MAX_VIDEO_MB}MB"
                    )
                f.write(chunk)
    return dest


def ffprobe_duration_sec(path: Path) -> float:
    """Best-effort duration via ffprobe; 0.0 if unavailable."""
    import json
    import subprocess

    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        fmt = json.loads(out.stdout or "{}").get("format", {})
        return float(fmt.get("duration", 0) or 0)
    except Exception:
        return 0.0


def extract_audio_only(video: Path, dest: Path) -> Path:
    """ffmpeg audio extraction for talking-head cost saver. Raises if ffmpeg missing."""
    import subprocess

    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video), "-vn",
         "-ac", "1", "-ar", "16000", str(dest)],
        check=True, capture_output=True, timeout=300,
    )
    return dest
