"""FastAPI webhook server: Telegram -> onboarding -> pipeline -> publishing."""
from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from . import config, drive, gemini, onboarding, pairing, platforms, publisher, scheduler, telegram, web_tokens
from . import web_ui
from .linkedin import publish_video_post
from .pipeline import run_material_pipeline, run_pipeline, run_rewrite
from .store import store

log = logging.getLogger("server")
app = FastAPI(title="Social Posting Agent")
app.include_router(web_ui.router)

MAX_TELEGRAM_UPLOAD = 20 * 1024 * 1024


@app.on_event("startup")
def _startup() -> None:
    scheduler.start_scheduler()


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


# --- text + media routing --------------------------------------------------

def handle_message(chat_id: int, user_id: int, msg: dict,
                   background: BackgroundTasks) -> None:
    text = (msg.get("text") or "").strip()
    state = store.get_state(user_id)

    # Commands that work before and after onboarding.
    if text.startswith("/start"):
        payload = text[len("/start"):].strip()
        if payload:
            _claim_site_session(chat_id, user_id, payload)
            return
        if state.onboarded:
            telegram.send_message(chat_id, _dashboard(state))
        else:
            # First contact or resume: offer the web form and the chat flow.
            onboarding.invite(chat_id, user_id)
        return
    if text.startswith("/settings"):
        telegram.send_message(chat_id, "Restarting setup. Your old profile is cleared.")
        onboarding.start(chat_id, user_id, reset=True)
        return
    if text.startswith("/help"):
        telegram.send_message(chat_id, _help_text())
        return
    if text.startswith("/status"):
        telegram.send_message(chat_id, _status_text(user_id))
        return
    if text.startswith("/new") or text.startswith("/cancel"):
        _reset_material(user_id)
        telegram.send_message(
            chat_id,
            "Cleared. Send material whenever you are ready." if state.onboarded
            else "Cleared. Send /start to set up.")
        return

    # First-time users always enter onboarding.
    if not state.onboarded:
        if state.state != "ONBOARDING":
            if state.invite_sent:
                # Second plain message: they want the in-chat setup.
                onboarding.start(chat_id, user_id, reset=False)
            else:
                onboarding.invite(chat_id, user_id)
            return
        if "voice" in msg or "audio" in msg:
            text = _transcribe(chat_id, msg)
            if text is None:
                return
        if msg.get("photo") or msg.get("video") or msg.get("document"):
            if not onboarding.handle_media(chat_id, user_id):
                telegram.send_message(chat_id, "Finish setup first.")
            return
        if text:
            onboarding.handle_text(chat_id, user_id, text, background)
        return

    # --- onboarded: edit revision? ---
    if state.state == "AWAITING_APPROVAL" and text and not (
            msg.get("photo") or msg.get("video") or msg.get("document")):
        latest = store.latest_pending(user_id)
        if latest:
            store.set_state(state.model_copy(update={"state": "EDITING"}))
            background.add_task(run_rewrite, chat_id, latest.draft_id, text)
            telegram.send_message(chat_id, "Rewriting with your edit...")
            return

    # --- voice note -> material / instructions ---
    if "voice" in msg or "audio" in msg:
        transcript = _transcribe(chat_id, msg)
        if transcript is None:
            return
        _accept_text(chat_id, user_id, transcript, background)
        return

    # --- media messages ---
    if msg.get("photo") or msg.get("video") or msg.get("document"):
        handle_media_message(chat_id, user_id, msg, background)
        return

    # --- Drive link -> video pipeline ---
    fid = drive.parse_drive_file_id(text) if text else None
    if fid:
        handle_link(chat_id, user_id, fid, background)
        return
    if text and "drive.google.com" in text:
        telegram.send_message(
            chat_id,
            "That doesn't look like a Google Drive link I can read. Use the "
            "Share link for a file (it contains /file/d/... or id=...), or "
            "send the file directly.")
        return

    if text:
        _accept_text(chat_id, user_id, text, background)


def _claim_site_session(chat_id: int, user_id: int, payload: str) -> None:
    """`/start <id>` from the public website's t.me deep link."""
    session = pairing.claim(payload, user_id)
    if not session:
        site = config.PUBLIC_BASE_URL.rstrip("/")
        hint = f" Start again at {site}." if site else ""
        telegram.send_message(
            chat_id, "That setup link is invalid or has expired." + hint)
        return
    state = store.get_state(user_id)
    if state.onboarded:
        telegram.send_message(chat_id, _dashboard(state))
        return
    if not state.platform:
        onboarding.set_platform(user_id, session["platform"])
    url = web_tokens.web_url(user_id)
    if not url:
        onboarding.resume(chat_id, user_id)
        return
    telegram.send_message(
        chat_id,
        "Telegram linked.\n\nYour setup continues in the browser - the page "
        "updates by itself. Or finish it right here in chat.",
        reply_markup={"inline_keyboard": [
            [{"text": "Continue setup", "url": url}],
            [{"text": "Do it here instead", "callback_data": "ob:chat"}],
        ]})


def _transcribe(chat_id: int, msg: dict) -> str | None:
    media = msg.get("voice") or msg.get("audio") or {}
    try:
        file_path = telegram.get_file(media["file_id"])
        tmp = Path(tempfile.mkdtemp(prefix="voice_")) / "voice.ogg"
        telegram.download_file(file_path, tmp)
        transcript = gemini.transcribe_voice(tmp)
        try:
            tmp.unlink()
            tmp.parent.rmdir()
        except OSError:
            pass
        return transcript
    except Exception as e:  # noqa: BLE001
        log.exception("voice transcribe failed")
        telegram.send_message(chat_id, f"Couldn't transcribe that: {e}")
        return None


def _accept_text(chat_id: int, user_id: int, text: str,
                 background: BackgroundTasks) -> None:
    s = store.get_state(user_id)
    if s.state == "AWAITING_APPROVAL":
        latest = store.latest_pending(user_id)
        if latest:
            store.set_state(s.model_copy(update={"state": "EDITING"}))
            background.add_task(run_rewrite, chat_id, latest.draft_id, text)
            telegram.send_message(chat_id, "Rewriting with your edit...")
            return
    if s.state == "AWAITING_LINK" and s.drive_file_id:
        store.set_state(s.model_copy(update={
            "state": "PROCESSING", "instructions": text}))
        background.add_task(run_pipeline, chat_id, s.drive_file_id, text)
        return
    if s.pending_media_file_id:
        if s.platform == "instagram" and text.startswith("http"):
            # Instagram cannot fetch Telegram's private file URLs: the public
            # URL becomes the media, and the next message is the caption.
            store.set_state(s.model_copy(update={
                "pending_media_file_id": "",
                "pending_media_url": text,
                "pending_media_type": s.pending_media_type or "image",
            }))
            telegram.send_message(
                chat_id,
                "Got the public URL. Now send the caption or idea for this "
                "post.")
            return
        _start_material(chat_id, user_id, text, background,
                        s.pending_media_file_id, s.pending_media_type)
        return
    if s.pending_media_url:
        _start_material(chat_id, user_id, text, background,
                        media_url=s.pending_media_url,
                        media_type="image" if s.platform == "instagram" else "text")
        return
    if s.platform == "instagram" and text.startswith("http"):
        store.set_state(s.model_copy(update={"pending_media_url": text}))
        telegram.send_message(
            chat_id, "Got the media URL. Send the caption or idea for this post.")
        return
    _start_material(chat_id, user_id, text, background)


def _start_material(chat_id: int, user_id: int, material: str,
                    background: BackgroundTasks, media_file_id: str = "",
                    media_type: str = "text", media_url: str = "") -> None:
    s = store.get_state(user_id)
    if s.state == "AWAITING_APPROVAL" and store.latest_pending(user_id):
        telegram.send_message(
            chat_id,
            "You already have a pending draft. Reply with edits, tap Approve, "
            "or send /new to discard it and start fresh.")
        return
    store.set_state(s.model_copy(update={
        "state": "PROCESSING",
        "pending_text": "",
        "pending_media_file_id": "",
        "pending_media_type": "",
        "pending_media_url": "",
    }))
    background.add_task(
        run_material_pipeline, chat_id, material, media_file_id, media_type,
        media_url)


def handle_media_message(chat_id: int, user_id: int, msg: dict,
                         background: BackgroundTasks) -> None:
    caption = (msg.get("caption") or "").strip()
    file_id, kind, size, mime = _media_info(msg)
    if not file_id:
        telegram.send_message(chat_id, "I could not read that file.")
        return
    if not kind:
        telegram.send_message(chat_id, "I can only handle image or video files.")
        return
    if size and size > MAX_TELEGRAM_UPLOAD:
        telegram.send_message(
            chat_id,
            "That file is over Telegram's 20 MB bot limit. For video, send a "
            "Google Drive link instead; for a large image, send a public URL.")
        return
    if size is None:
        try:
            info = telegram.get_file_info(file_id)
            if int(info.get("file_size", 0) or 0) > MAX_TELEGRAM_UPLOAD:
                telegram.send_message(
                    chat_id,
                    "That file is over Telegram's 20 MB bot limit. Send a "
                    "Google Drive link for video.")
                return
        except Exception:  # noqa: BLE001
            pass

    if caption and kind == "video" and drive.parse_drive_file_id(caption):
        handle_link(chat_id, user_id, drive.parse_drive_file_id(caption), background)
        return

    if kind == "image" and s_platform(user_id) == "instagram" and not caption:
        store.set_state(store.get_state(user_id).model_copy(update={
            "pending_media_file_id": file_id,
            "pending_media_type": "image",
        }))
        telegram.send_message(
            chat_id,
            "Got the image. Instagram needs a public image URL too - send "
            "one, or switch platform in /settings.")
        return

    if caption:
        _start_material(chat_id, user_id, caption, background,
                        file_id, kind)
        return
    store.set_state(store.get_state(user_id).model_copy(update={
        "pending_media_file_id": file_id,
        "pending_media_type": kind,
    }))
    if kind == "image":
        telegram.send_message(
            chat_id,
            "Got the image. Send the caption or idea for this post. "
            "For full quality, send images as a file instead of a photo.")
    else:
        telegram.send_message(
            chat_id,
            "Got the video. Send the angle/topic for the post. I will analyse "
            "it and draft in your voice.")


def s_platform(user_id: int) -> str:
    return store.get_state(user_id).platform


def _media_info(msg: dict) -> tuple[str, str, int | None, str]:
    if msg.get("photo"):
        p = msg["photo"][-1]
        return p.get("file_id", ""), "image", p.get("file_size"), "image/jpeg"
    if msg.get("video"):
        v = msg["video"]
        return v.get("file_id", ""), "video", v.get("file_size"), v.get("mime_type", "video/mp4")
    doc = msg.get("document") or {}
    mime = doc.get("mime_type", "")
    kind = "image" if mime.startswith("image/") else "video" if mime.startswith("video/") else ""
    return doc.get("file_id", ""), kind, doc.get("file_size"), mime


# --- Drive video flow ------------------------------------------------------

def handle_link(chat_id: int, user_id: int, file_id: str,
                background: BackgroundTasks) -> None:
    state = store.get_state(user_id)
    if store.latest_pending(user_id):
        telegram.send_message(
            chat_id, "You have a pending draft. /new to discard before starting "
                     "a new video.")
        return
    try:
        ok, name_or_err, size = drive.check_access(file_id)
    except Exception as e:  # noqa: BLE001
        log.exception("drive check failed")
        telegram.send_message(chat_id, f"Drive check failed: {e}")
        return
    if not ok:
        telegram.send_message(chat_id, name_or_err)  # stays put
        return
    size_mb = size / (1024 * 1024) if size else 0
    store.set_state(state.model_copy(update={
        "state": "AWAITING_LINK", "drive_file_id": file_id,
        "instructions": ""}))
    telegram.send_message(
        chat_id,
        f"Found: {name_or_err} ({size_mb:.1f} MB).\n\n"
        "Any angle or style instructions for this video? Send text or a voice "
        "note, or tap Skip.",
        reply_markup=telegram.skip_keyboard(),
    )


# --- callbacks -------------------------------------------------------------

def handle_callback(chat_id: int, user_id: int, data: str,
                    background: BackgroundTasks) -> None:
    if onboarding.handle_callback(chat_id, user_id, data, background):
        return
    state = store.get_state(user_id)
    if data == "skip_instructions":
        fid = state.drive_file_id
        if not fid:
            telegram.send_message(chat_id, "Send the Drive link first.")
            return
        store.set_state(state.model_copy(update={"state": "PROCESSING"}))
        background.add_task(run_pipeline, chat_id, fid, state.instructions)
        return

    kind, _, draft_id = data.partition(":")
    d = store.get_draft(draft_id) if draft_id else None

    if kind == "approve":
        if not d or d.status != "pending":
            telegram.send_message(chat_id, "That draft is expired or already handled.")
            return
        # Idempotent publish: mark approved FIRST so a double-tap cannot repost.
        d.status = "approved"
        store.save_draft(d)
        try:
            url = _publish(d)
            d.status = "published"
            d.post_url = url
            store.save_draft(d)
            store.set_state(state.model_copy(update={
                "state": "AWAITING_MATERIAL" if state.onboarded else "PUBLISHED"}))
            telegram.send_message(chat_id, f"Published{f': {url}' if url else '.'}")
        except Exception as e:  # noqa: BLE001
            log.exception("publish failed")
            d.status = "failed"
            store.save_draft(d)
            store.set_state(state.model_copy(update={"state": "FAILED"}))
            telegram.send_message(chat_id, f"Publish failed: {e}")
        return

    if kind == "sched":
        if not d or d.status != "pending":
            telegram.send_message(chat_id, "That draft is expired or already handled.")
            return
        try:
            when = scheduler.next_slot(state.schedule, state.timezone)
        except Exception as e:  # noqa: BLE001
            telegram.send_message(chat_id, f"Cannot schedule: {e}. Use /settings.")
            return
        d.status = "scheduled"
        d.scheduled_for = when
        d.expires_at = max(d.expires_at, when + timedelta(days=7))
        store.save_draft(d)
        local = when.astimezone(ZoneInfo(state.timezone))
        store.set_state(state.model_copy(update={"state": "AWAITING_MATERIAL"}))
        telegram.send_message(
            chat_id,
            f"Scheduled for {local:%a %d %b %Y, %H:%M} ({state.timezone}).")
        return

    if kind == "reject":
        if d:
            d.status = "rejected"
            store.save_draft(d)
        store.set_state(state.model_copy(update={"state": "AWAITING_MATERIAL"}))
        telegram.send_message(chat_id, "Discarded. Send new material anytime.")
        return

    if kind == "edit":
        store.set_state(state.model_copy(update={"state": "EDITING"}))
        telegram.send_message(chat_id, "Send your revision as voice or text.")
        return


def _publish(d) -> str:
    """Publish a draft, keeping the legacy video path testable."""
    if d.platform:
        return publisher.publish_draft(d)
    # Legacy drafts (created before onboarding) only have a Drive video.
    tmpdir = Path(tempfile.mkdtemp(prefix="pub_"))
    try:
        video = tmpdir / "video.mp4"
        drive.download_file(d.drive_file_id, video)
        return publish_video_post(d.draft_text, video)
    finally:
        try:
            for p in tmpdir.glob("*"):
                p.unlink()
            tmpdir.rmdir()
        except OSError:
            pass


# --- small helpers ---------------------------------------------------------

def _reset_material(user_id: int) -> None:
    s = store.get_state(user_id)
    latest = store.latest_pending(user_id)
    if latest:
        latest.status = "rejected"
        store.save_draft(latest)
    store.set_state(s.model_copy(update={
        "state": "AWAITING_MATERIAL" if s.onboarded else "IDLE",
        "instructions": "", "drive_file_id": "",
        "pending_text": "", "pending_media_file_id": "",
        "pending_media_type": "", "pending_media_url": "",
    }))


def _dashboard(s) -> str:
    p = platforms.get(s.platform)
    mode = (f"Scheduled - {s.schedule} ({s.timezone})"
            if s.posting_mode == "scheduled" else "On demand")
    return (
        "You are set up.\n\n"
        f"Platform: {p.name if p else s.platform}\n"
        f"Account: {s.account_label or s.account or 'not set'}\n"
        f"Mode: {mode}\n"
        f"Media: {s.media_pref}\n\n"
        "Send an idea, photo, video, voice note, or Drive link. "
        "Commands: /status /settings /new")


def _status_text(user_id: int) -> str:
    s = store.get_state(user_id)
    if not s.onboarded:
        return (f"Onboarding step: {s.onboarding_step or 'not started'} "
                f"({len(s.samples)} samples). Send /start to continue.")
    p = platforms.get(s.platform)
    lines = [
        f"Platform: {p.name if p else s.platform}",
        f"Account: {s.account_label or s.account or 'not set'}",
        f"Posts: {'scheduled ' + s.schedule + ' (' + s.timezone + ')' if s.posting_mode == 'scheduled' else 'on demand'}",
        f"Media: {s.media_pref}",
        f"State: {s.state}",
    ]
    latest = store.latest_pending(user_id)
    if latest:
        lines.append(f"Pending draft: {latest.draft_id}")
    return "\n".join(lines)


def _help_text() -> str:
    return (
        "Send material (text, photo, video, voice, or Drive link) and I draft "
        "it in your voice.\n\n"
        "/start - dashboard (or your web setup link)\n"
        "/settings - redo platform, voice, mode, media\n"
        "/status - current profile and pending draft\n"
        "/new - discard the current draft\n"
        "/cancel - same as /new")
