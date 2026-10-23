"""Background pipeline: INGEST → ANALYZE → WRITE → REVIEW. Never block webhook."""
from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import config, drive, gemini, telegram
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


def format_draft_message(draft_text: str, drive_file_id: str,
                         verify: list[str]) -> str:
    lines = [draft_text, "",
             f"Video: https://drive.google.com/file/d/{drive_file_id}/view"]
    if verify:
        lines += ["", "Verify:"]
        lines += [f"- {v}" for v in verify[:12]]
    return "\n".join(lines)


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

        telegram.send_message(chat_id, "Writing draft...")
        draft = gemini.write_post(analysis, instructions)

        record = DraftRecord(
            draft_id=new_draft_id(),
            telegram_id=chat_id,
            drive_file_id=file_id,
            analysis_json=analysis,
            draft_text=draft.full_text(),
            status="pending",
            created_at=datetime.now(timezone.utc),
            expires_at=expiry(),
        )
        store.save_draft(record)
        store.set_state(store.get_state(chat_id).model_copy(update={
            "state": "AWAITING_APPROVAL", "drive_file_id": file_id,
        }))
        telegram.send_message(
            chat_id,
            format_draft_message(record.draft_text, file_id, draft.verify_block),
            reply_markup=telegram.approval_keyboard(record.draft_id),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("pipeline failed")
        st = store.get_state(chat_id)
        store.set_state(st.model_copy(update={"state": "FAILED"}))
        telegram.send_message(
            chat_id,
            f"Something failed: {e}\nSend /new to start over, or send the Drive link again.",
        )
    finally:
        _cleanup(video_path, audio_path)
        try:
            tmpdir.rmdir()
        except OSError:
            pass


def run_rewrite(chat_id: int, draft_id: str, new_instructions: str) -> None:
    """Edit path: re-run WRITE on stored analysis only (no re-analysis)."""
    d = store.get_draft(draft_id)
    if not d or d.status != "pending":
        telegram.send_message(chat_id, "That draft is no longer pending. Send /new to start over.")
        return
    try:
        combined = (store.get_state(chat_id).instructions + "\n" + new_instructions).strip()
        draft = gemini.write_post(d.analysis_json, combined)
        record = DraftRecord(
            draft_id=new_draft_id(),
            telegram_id=chat_id,
            drive_file_id=d.drive_file_id,
            analysis_json=d.analysis_json,
            draft_text=draft.full_text(),
            status="pending",
            created_at=datetime.now(timezone.utc),
            expires_at=expiry(),
        )
        d.status = "rejected"
        store.save_draft(d)
        store.save_draft(record)
        telegram.send_message(
            chat_id,
            format_draft_message(record.draft_text, record.drive_file_id,
                                 draft.verify_block),
            reply_markup=telegram.approval_keyboard(record.draft_id),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("rewrite failed")
        telegram.send_message(chat_id, f"Rewrite failed: {e}")
