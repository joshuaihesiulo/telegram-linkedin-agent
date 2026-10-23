"""Publish a stored draft to its platform, downloading media only when needed."""
from __future__ import annotations

import tempfile
from pathlib import Path

from . import drive, platforms, telegram
from .schemas import DraftRecord
from .store import store


def publish_draft(d: DraftRecord) -> str:
    state = store.get_state(d.telegram_id)
    platform = d.platform or state.platform
    account = state.account
    if not platform:
        raise RuntimeError("No platform selected. Run /settings.")
    if not account and platform != "twitter":
        raise RuntimeError("No posting account selected. Run /settings.")

    tmpdir = Path(tempfile.mkdtemp(prefix="pub_"))
    media_path: Path | None = None
    try:
        if d.media_file_id:
            info = telegram.get_file_info(d.media_file_id)
            suffix = ".jpg" if d.media_type == "image" else ".mp4"
            media_path = tmpdir / f"media{suffix}"
            telegram.download_file(info["file_path"], media_path)
        elif d.drive_file_id:
            media_path = tmpdir / "video.mp4"
            drive.download_file(d.drive_file_id, media_path)

        media_url = d.media_url
        if platform == "instagram" and not media_url:
            raise RuntimeError(
                "Instagram needs a public image/video URL. Send the post with "
                "a public URL.")
        return platforms.publish(
            user_id=d.telegram_id,
            platform=platform,
            account=account,
            text=d.draft_text,
            media_path=media_path,
            media_type=d.media_type or "text",
            media_url=media_url,
        )
    finally:
        try:
            for p in tmpdir.glob("*"):
                p.unlink()
            tmpdir.rmdir()
        except OSError:
            pass
