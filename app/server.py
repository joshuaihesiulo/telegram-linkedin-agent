"""FastAPI webhook server: Telegram → state machine → background crew."""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from . import config, drive, gemini, telegram
from .linkedin import publish_video_post
from .pipeline import run_pipeline, run_rewrite
from .store import store

log = logging.getLogger("server")
app = FastAPI(title="LinkedIn Video-to-Post Agent")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


def _allowed(telegram_id: int) -> bool:
    if not config.ALLOWED_TELEGRAM_USER_ID:
        return True  # single-user gate unset in local dev
    try:
        return int(telegram_id) == int(config.ALLOWED_TELEGRAM_USER_ID)
    except ValueError:
        return str(telegram_id) == config.ALLOWED_TELEGRAM_USER_ID


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks,
                  x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if config.TELEGRAM_WEBHOOK_SECRET:
        if x_telegram_bot_api_secret_token != config.TELEGRAM_WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="bad secret")
    update = await request.json()
    update_id = update.get("update_id")
    if update_id is not None and store.seen_update(int(update_id)):
        return {"ok": True, "deduped": True}

    # --- callbacks (buttons) ---
    if "callback_query" in update:
        cb = update["callback_query"]
        from_id = cb.get("from", {}).get("id")
        if not _allowed(int(from_id)):
            return {"ok": True, "ignored": True}
        chat_id = cb["message"]["chat"]["id"]
        data = cb.get("data", "")
        telegram.answer_callback(str(cb.get("id", "")))
        handle_callback(chat_id, int(from_id), data, background)
        return {"ok": True}

    msg = update.get("message") or {}
    from_id = (msg.get("from") or {}).get("id")
    if from_id is None:
        return {"ok": True}
    if not _allowed(int(from_id)):
        return {"ok": True, "ignored": True}  # silent ignore per acceptance test
    chat_id = msg["chat"]["id"]
    handle_message(chat_id, int(from_id), msg, background)
    return {"ok": True}


def handle_message(chat_id: int, user_id: int, msg: dict,
                   background: BackgroundTasks) -> None:
    text = (msg.get("text") or "").strip()
    state = store.get_state(user_id)

    # --- commands ---
    if text.startswith("/start"):
        store.set_state(state.model_copy(update={
            "state": "IDLE", "instructions": "", "drive_file_id": ""}))
        telegram.send_message(
            chat_id, "Hi. Send a voice note or text with style/angle/audience "
                     "instructions and I'll ask for the Drive link.")
        return
    if text.startswith("/new") or text.startswith("/cancel"):
        store.set_state(state.model_copy(update={
            "state": "IDLE", "instructions": "", "drive_file_id": ""}))
        telegram.send_message(chat_id, "Reset. Send instructions or a Drive link.")
        return
    if text.startswith("/status"):
        pending = ""
        try:
            latest = store.latest_pending(user_id)
            if latest:
                pending = f" Pending draft {latest.draft_id} ({latest.status})."
        except Exception:
            pass
        telegram.send_message(
            chat_id, f"State: {state.state}.{pending}")
        return

    # --- voice note → transcribe to instructions ---
    if "voice" in msg or "audio" in msg:
        media = msg.get("voice") or msg.get("audio") or {}
        try:
            file_path = telegram.get_file(media["file_id"])
            tmp = Path(tempfile.mkdtemp(prefix="voice_")) / "voice.ogg"
            telegram.download_file(file_path, tmp)
            instructions = gemini.transcribe_voice(tmp)
            try:
                tmp.unlink()
                tmp.parent.rmdir()
            except OSError:
                pass
        except Exception as e:  # noqa: BLE001
            log.exception("voice transcribe failed")
            telegram.send_message(chat_id, f"Couldn't transcribe that: {e}")
            return
        handle_instructions(chat_id, user_id, instructions)
        return

    # --- Drive link? ---
    fid = drive.parse_drive_file_id(text) if text else None
    if fid:
        handle_link(chat_id, user_id, fid, background)
        return
    if text and text.startswith("http"):
        telegram.send_message(
            chat_id, "That doesn't look like a Google Drive link. "
                     "Please send the share link to the video.")
        return

    # --- plain text: instructions or edit revision ---
    if text:
        if state.state == "AWAITING_APPROVAL" and state.drive_file_id:
            # Treat as edit revision if a pending draft exists.
            latest = store.latest_pending(user_id)
            if latest:
                store.set_state(state.model_copy(update={"state": "EDITING"}))
                background.add_task(run_rewrite, chat_id, latest.draft_id, text)
                telegram.send_message(chat_id, "Rewriting with your edit...")
                return
        handle_instructions(chat_id, user_id, text)
        return


def handle_instructions(chat_id: int, user_id: int, instructions: str) -> None:
    state = store.get_state(user_id)
    if state.state == "EDITING":
        latest = store.latest_pending(user_id)
        if latest:
            from .pipeline import run_rewrite as _rw

            _rw(chat_id, latest.draft_id, instructions)
            return
    store.set_state(state.model_copy(update={
        "state": "AWAITING_LINK", "instructions": instructions}))
    telegram.send_message(
        chat_id, "Got it. Now send me the Google Drive link to the video.")


def handle_link(chat_id: int, user_id: int, file_id: str,
                background: BackgroundTasks) -> None:
    state = store.get_state(user_id)
    if state.state == "IDLE" and not state.instructions:
        store.set_state(state.model_copy(update={
            "state": "AWAITING_LINK", "drive_file_id": file_id}))
        telegram.send_message(
            chat_id, "Any style or angle instructions? Send a voice note or "
                     "text, or tap Skip.",
            reply_markup=telegram.skip_keyboard())
        return
    try:
        ok, name_or_err, size = drive.check_access(file_id)
    except Exception as e:  # noqa: BLE001
        log.exception("drive check failed")
        telegram.send_message(chat_id, f"Drive check failed: {e}")
        return
    if not ok:
        telegram.send_message(chat_id, name_or_err)  # stays in AWAITING_LINK
        return
    size_mb = size / (1024 * 1024) if size else 0
    dur = 0.0  # duration known after download; reported in progress
    telegram.send_message(
        chat_id, f"Found: {name_or_err} ({size_mb:.1f} MB). Processing...")
    store.set_state(state.model_copy(update={
        "state": "PROCESSING", "drive_file_id": file_id}))
    background.add_task(run_pipeline, chat_id, file_id, state.instructions)


def handle_callback(chat_id: int, user_id: int, data: str,
                    background: BackgroundTasks) -> None:
    state = store.get_state(user_id)
    if data == "skip_instructions":
        fid = state.drive_file_id
        if not fid:
            telegram.send_message(chat_id, "Send the Drive link first.")
            return
        try:
            ok, _, _ = drive.check_access(fid)
        except Exception as e:  # noqa: BLE001
            telegram.send_message(chat_id, f"Drive check failed: {e}")
            return
        if not ok:
            telegram.send_message(chat_id, "I still can't open that file.")
            return
        store.set_state(state.model_copy(update={"state": "PROCESSING"}))
        background.add_task(run_pipeline, chat_id, fid, "")
        return
    kind, _, draft_id = data.partition(":")
    d = store.get_draft(draft_id) if draft_id else None
    if kind == "approve":
        if not d or d.status != "pending":
            telegram.send_message(chat_id, "That draft is expired or already handled.")
            return
        # Idempotent publish: mark approved FIRST so a double-tap can't repost.
        d.status = "approved"
        store.save_draft(d)
        try:
            tmpdir = Path(tempfile.mkdtemp(prefix="pub_"))
            video = tmpdir / "video.mp4"
            drive.download_file(d.drive_file_id, video)
            url = publish_video_post(d.draft_text, video)
            d.status = "published"
            d.post_url = url
            store.save_draft(d)
            store.set_state(state.model_copy(update={"state": "PUBLISHED"}))
            telegram.send_message(chat_id, f"Published: {url}")
        except Exception as e:  # noqa: BLE001
            log.exception("publish failed")
            d.status = "failed"
            store.save_draft(d)
            store.set_state(state.model_copy(update={"state": "FAILED"}))
            telegram.send_message(chat_id, f"Publish failed: {e}")
        finally:
            try:
                for p in tmpdir.glob("*"):
                    p.unlink()
                tmpdir.rmdir()
            except OSError:
                pass
        return
    if kind == "reject":
        if d:
            d.status = "rejected"
            store.save_draft(d)
        store.set_state(state.model_copy(update={
            "state": "IDLE", "instructions": "", "drive_file_id": ""}))
        telegram.send_message(chat_id, "Discarded. Send /new to start over.")
        return
    if kind == "edit":
        store.set_state(state.model_copy(update={"state": "EDITING"}))
        telegram.send_message(chat_id, "Send your revision as voice or text.")
        return
