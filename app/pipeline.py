"""Background pipeline: INGEST -> ANALYZE -> WRITE -> REVIEW. Never block webhook."""
from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import config, drive, gemini, platforms, telegram
from .schemas import DraftRecord
from .store import expiry, new_draft_id, store

log = logging.getLogger("pipeline")


def _cleanup(*paths: Path) -> None:
    for p in paths:
        try:
            if p and Path(p).exists():
                Path(p).unlink()
        except OSError:
            pass


def _profile(chat_id: int):
    s = store.get_state(chat_id)
    platform = platforms.get(s.platform)
    return s, platform


def format_draft_message(draft_text: str, drive_file_id: str,
                         verify: list[str], platform: str = "",
                         media_type: str = "text") -> str:
    lines = [draft_text, ""]
    if platform:
        lines.append(f"Platform: {platform}")
    if drive_file_id:
        lines.append(f"Video: https://drive.google.com/file/d/{drive_file_id}/view")
    if media_type and media_type != "text":
        lines.append(f"Media: {media_type} attached")
    if verify:
        lines += ["", "Verify:"]
        lines += [f"- {v}" for v in verify[:12]]
    return "\n".join(lines)


def _send_draft(chat_id: int, record: DraftRecord, verify: list[str]) -> None:
    s = store.get_state(chat_id)
    p = platforms.get(record.platform)
    label = p.name if p else record.platform
    telegram.send_message(
        chat_id,
        format_draft_message(record.draft_text, record.drive_file_id, verify,
                             label, record.media_type),
        reply_markup=telegram.approval_keyboard(
            record.draft_id, can_schedule=s.posting_mode == "scheduled"),
    )


def _write(chat_id: int, analysis, instructions: str, platform: str,
           max_chars: int):
    s, _ = _profile(chat_id)
    return gemini.write_post(
        analysis, instructions,
        style=s.voice_guide or gemini.DEFAULT_STYLE,
        platform=platform, max_chars=max_chars, samples=s.samples,
    )


def run_pipeline(chat_id: int, file_id: str, instructions: str) -> None:
    """Full run for a new Drive link. Sends progress + draft to Telegram."""
    tmpdir = Path(tempfile.mkdtemp(prefix="v2p_"))
    video_path = tmpdir / "video.mp4"
    work_path = video_path
    audio_path = tmpdir / "audio.mp3"
    try:
        telegram.send_message(chat_id, "Downloading...")
        drive.download_file(file_id, video_path)

        # Audio-only default for large/talking-head videos (cost saver).
        if config.AUDIO_ONLY_DEFAULT:
            try:
                telegram.send_message(chat_id, "Extracting audio (cost saver)...")
                drive.extract_audio_only(video_path, audio_path)
                work_path = audio_path
            except Exception as e:  # noqa: BLE001
                log.warning("audio extract failed, using full video: %s", e)

        telegram.send_message(chat_id, "Analyzing video...")
        analysis = gemini.analyze_video(work_path, instructions)

        s, p = _profile(chat_id)
        platform = p.name if p else "Social"
        max_chars = p.max_chars if p else 3000
        telegram.send_message(chat_id, "Writing draft...")
        draft = _write(chat_id, analysis, instructions, platform, max_chars)

        record = DraftRecord(
            draft_id=new_draft_id(),
            telegram_id=chat_id,
            drive_file_id=file_id,
            analysis_json=analysis,
            draft_text=draft.full_text(),
            status="pending",
            created_at=datetime.now(timezone.utc),
            expires_at=expiry(),
            platform=s.platform,
            media_type="video",
        )
        store.save_draft(record)
        store.set_state(store.get_state(chat_id).model_copy(update={
            "state": "AWAITING_APPROVAL", "drive_file_id": file_id,
        }))
        _send_draft(chat_id, record, draft.verify_block)
    except Exception as e:  # noqa: BLE001
        log.exception("pipeline failed")
        st = store.get_state(chat_id)
        store.set_state(st.model_copy(update={"state": "FAILED"}))
        telegram.send_message(
            chat_id,
            f"Something failed: {e}\nSend /new to start over, or resend the link.",
        )
    finally:
        _cleanup(video_path, audio_path)
        try:
            tmpdir.rmdir()
        except OSError:
            pass


def run_material_pipeline(chat_id: int, material: str,
                          media_file_id: str = "", media_type: str = "text",
                          media_url: str = "") -> None:
    """Draft from text, an uploaded image/video, or a public URL."""
    tmpdir = Path(tempfile.mkdtemp(prefix="mat_"))
    try:
        s, p = _profile(chat_id)
        platform = p.name if p else "Social"
        max_chars = p.max_chars if p else 3000
        analysis = None
        text = material

        if media_type == "video" and media_file_id:
            telegram.send_message(chat_id, "Downloading video...")
            info = telegram.get_file_info(media_file_id)
            path = tmpdir / "video.mp4"
            telegram.download_file(info["file_path"], path)
            telegram.send_message(chat_id, "Analyzing video...")
            analysis = gemini.analyze_video(path, material)
            draft = _write(chat_id, analysis, material, platform, max_chars)
        else:
            telegram.send_message(chat_id, "Writing draft...")
            draft = gemini.write_material_post(
                text, style=s.voice_guide or gemini.DEFAULT_STYLE,
                platform=platform, max_chars=max_chars, samples=s.samples)

        record = DraftRecord(
            draft_id=new_draft_id(),
            telegram_id=chat_id,
            draft_text=draft.full_text(),
            status="pending",
            created_at=datetime.now(timezone.utc),
            expires_at=expiry(),
            analysis_json=analysis,
            platform=s.platform,
            media_type=media_type,
            media_file_id=media_file_id,
            media_url=media_url,
        )
        store.save_draft(record)
        store.set_state(store.get_state(chat_id).model_copy(update={
            "state": "AWAITING_APPROVAL",
            "instructions": material,
            "pending_text": "", "pending_media_file_id": "",
            "pending_media_type": "", "pending_media_url": "",
        }))
        _send_draft(chat_id, record, draft.verify_block)
    except Exception as e:  # noqa: BLE001
        log.exception("material pipeline failed")
        store.set_state(store.get_state(chat_id).model_copy(update={
            "state": "FAILED"}))
        telegram.send_message(chat_id, f"Draft failed: {e}")
    finally:
        try:
            for p in tmpdir.glob("*"):
                p.unlink()
            tmpdir.rmdir()
        except OSError:
            pass


def run_rewrite(chat_id: int, draft_id: str, new_instructions: str) -> None:
    """Edit path: re-run WRITE on stored analysis only (no re-analysis)."""
    d = store.get_draft(draft_id)
    if not d or d.status != "pending":
        telegram.send_message(chat_id, "That draft is no longer pending. Send /new.")
        return
    try:
        s, p = _profile(chat_id)
        combined = (store.get_state(chat_id).instructions + "\n" + new_instructions).strip()
        platform = p.name if p else "Social"
        max_chars = p.max_chars if p else 3000
        if d.analysis_json:
            draft = _write(chat_id, d.analysis_json, combined, platform, max_chars)
        else:
            draft = gemini.write_material_post(
                combined, style=s.voice_guide or gemini.DEFAULT_STYLE,
                platform=platform, max_chars=max_chars, samples=s.samples)
        record = DraftRecord(
            draft_id=new_draft_id(),
            telegram_id=chat_id,
            drive_file_id=d.drive_file_id,
            analysis_json=d.analysis_json,
            draft_text=draft.full_text(),
            status="pending",
            created_at=datetime.now(timezone.utc),
            expires_at=expiry(),
            platform=d.platform or s.platform,
            media_type=d.media_type,
            media_file_id=d.media_file_id,
            media_url=d.media_url,
        )
        d.status = "rejected"
        store.save_draft(d)
        store.save_draft(record)
        _send_draft(chat_id, record, draft.verify_block)
    except Exception as e:  # noqa: BLE001
        log.exception("rewrite failed")
        telegram.send_message(chat_id, f"Rewrite failed: {e}")
