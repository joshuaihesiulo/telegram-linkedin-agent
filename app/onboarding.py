"""First-time-user onboarding state machine.

Flow: platform -> Connect Link -> account -> 10 samples -> voice profile ->
posting mode/schedule -> media preference -> live.

This module stays transport-agnostic except for Telegram send helpers, so the
webhook only has to route messages and callbacks here.
"""
from __future__ import annotations

import logging

from . import config, gemini, platforms, telegram
from .store import store

log = logging.getLogger("onboarding")

SAMPLE_TARGET = max(1, config.ONBOARDING_SAMPLES)


def _state(user_id: int):
    return store.get_state(user_id)


def _save(user_id: int, **updates):
    s = store.get_state(user_id)
    store.set_state(s.model_copy(update=updates))


def _count(user_id: int) -> int:
    return len(_state(user_id).samples)


def _retry_keyboard(slug: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "Try again", "callback_data": f"ob:platform:{slug}"},
    ]]}


def start(chat_id: int, user_id: int, reset: bool = True) -> None:
    """Begin (or restart) onboarding."""
    updates = {
        "state": "ONBOARDING",
        "onboarding_step": "platform",
        "onboarded": False,
        "connect_url": "",
        "pending_text": "",
        "pending_media_file_id": "",
        "pending_media_type": "",
        "pending_media_url": "",
        "instructions": "",
        "drive_file_id": "",
    }
    if reset:
        updates.update({
            "platform": "", "account": "", "account_label": "",
            "samples": [], "voice_guide": "", "posting_mode": "on_demand",
            "schedule": "", "timezone": config.DEFAULT_TIMEZONE,
            "media_pref": "text",
        })
    _save(user_id, **updates)
    telegram.send_message(
        chat_id,
        "Welcome! I am your social-media posting agent.\n\n"
        "I will set you up in six steps: platform, account, your voice, "
        "posting rhythm, media, then we go live.\n\n"
        "Step 1 of 6 - Where do you want to post?",
        reply_markup=telegram.platform_keyboard(platforms.choices()),
    )


def resume(chat_id: int, user_id: int) -> None:
    """Re-prompt the current step without clearing progress (used by /start).

    Onboarding continues automatically when the user sends their next message;
    this just shows where they are. /settings is the explicit full restart.
    """
    s = _state(user_id)
    if s.onboarded:
        return
    p = platforms.get(s.platform) if s.platform else None
    step = s.onboarding_step or "platform"

    if not p or step == "platform":
        start(chat_id, user_id, reset=False)
    elif step == "connect":
        _begin_connect(chat_id, user_id)
    elif step == "account":
        _present_accounts(chat_id, user_id)
    elif step == "samples":
        _ask_samples(chat_id, user_id)
    elif step == "analyzing":
        telegram.send_message(chat_id, "Still learning your voice - one moment.")
    elif step == "mode":
        _ask_mode(chat_id, user_id)
    elif step == "schedule":
        _ask_schedule(chat_id, user_id)
    elif step == "media":
        _ask_media(chat_id, user_id)
    else:
        start(chat_id, user_id, reset=False)


def handle_callback(chat_id: int, user_id: int, data: str,
                    background) -> bool:
    if not data.startswith("ob:"):
        return False
    parts = data.split(":", 2)
    kind = parts[1] if len(parts) > 1 else ""

    if kind == "noop":
        return True
    if kind == "platform" and len(parts) == 3:
        _select_platform(chat_id, user_id, parts[2], background)
        return True
    if kind == "connected":
        telegram.send_message(chat_id, "Checking your connection...")
        background.add_task(_check_connected, chat_id, user_id)
        return True
    if kind == "newlink":
        _save(user_id, onboarding_step="connect")
        background.add_task(_begin_connect, chat_id, user_id)
        return True
    if kind == "acct" and len(parts) == 3:
        _accept_account(chat_id, user_id, parts[2], background)
        return True
    if kind == "samples_done":
        _samples_done(chat_id, user_id, background)
        return True
    if kind == "mode" and len(parts) == 3:
        _select_mode(chat_id, user_id, parts[2], background)
        return True
    if kind == "media" and len(parts) == 3:
        _select_media(chat_id, user_id, parts[2])
        return True
    return True


def handle_text(chat_id: int, user_id: int, text: str, background) -> bool:
    """Handle a text message while onboarding. Returns True when consumed."""
    s = _state(user_id)
    step = s.onboarding_step

    if step == "samples":
        if text.strip().lower() in ("/done", "done", "finish"):
            _samples_done(chat_id, user_id, background)
        elif text.startswith("/"):
            telegram.send_message(
                chat_id,
                f"Paste a past post as plain text ({_count(user_id)}/"
                f"{SAMPLE_TARGET} collected). Send /done when finished.")
        else:
            _add_sample(chat_id, user_id, text)
        return True

    if step == "account":
        _accept_account(chat_id, user_id, text.strip(), background)
        return True

    if step == "schedule":
        _handle_schedule(chat_id, user_id, text, background)
        return True

    if step == "connect":
        if text.strip().lower() in ("connected", "done", "i've connected"):
            background.add_task(_check_connected, chat_id, user_id)
            telegram.send_message(chat_id, "Checking your connection...")
        else:
            telegram.send_message(
                chat_id,
                "Open the Connect Link above, finish signing in, then tap "
                "I've connected (or send 'connected').")
        return True

    if step == "platform":
        telegram.send_message(chat_id, "Pick a platform with the buttons above.")
        return True
    if step == "mode":
        telegram.send_message(chat_id, "Pick Scheduled or Only when I send material.")
        return True
    if step == "media":
        telegram.send_message(chat_id, "Pick a media option with the buttons above.")
        return True
    if step == "analyzing":
        telegram.send_message(chat_id, "Still learning your voice - one moment.")
        return True
    return False


def handle_media(chat_id: int, user_id: int) -> bool:
    s = _state(user_id)
    if s.onboarding_step == "samples":
        telegram.send_message(
            chat_id,
            "Please paste your past posts as text for voice calibration. "
            f"{_count(user_id)}/{SAMPLE_TARGET} collected.")
        return True
    return False


# --- platform + connection -------------------------------------------------

def _select_platform(chat_id: int, user_id: int, slug: str, background) -> None:
    p = platforms.get(slug)
    if not p:
        telegram.send_message(chat_id, "Unknown platform. Pick one below.",
                              reply_markup=telegram.platform_keyboard(
                                  platforms.choices()))
        return
    _save(user_id, platform=p.slug, onboarding_step="connect", connect_url="")
    telegram.send_message(chat_id, f"Connecting {p.name}...")
    background.add_task(_begin_connect, chat_id, user_id)


def _begin_connect(chat_id: int, user_id: int) -> None:
    s = _state(user_id)
    p = platforms.get(s.platform)
    if not p:
        telegram.send_message(chat_id, "Pick a platform first.",
                              reply_markup=telegram.platform_keyboard(
                                  platforms.choices()))
        return
    try:
        if platforms.is_connected(user_id, p.slug):
            _after_connect(chat_id, user_id)
            return
        url = platforms.connect_url(user_id, p.slug)
        _save(user_id, connect_url=url, onboarding_step="connect")
        note = f"\n\nNote: {p.note}" if p.note else ""
        telegram.send_message(
            chat_id,
            f"Step 2 of 6 - Sign in to {p.name}.\n\n"
            f"Tap Connect account, finish the sign-in, then tap I've connected."
            f"{note}",
            reply_markup=telegram.connect_keyboard(url),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("connect start failed")
        telegram.send_message(
            chat_id,
            f"I could not start the {p.name} connection: {e}\n\n"
            "Check COMPOSIO_API_KEY, then tap Try again.",
            reply_markup=_retry_keyboard(p.slug),
        )


def _check_connected(chat_id: int, user_id: int) -> None:
    s = _state(user_id)
    p = platforms.get(s.platform)
    if not p:
        return
    try:
        if platforms.is_connected(user_id, p.slug):
            _after_connect(chat_id, user_id)
        else:
            if s.connect_url:
                msg = ("I still do not see an active connection. Finish the "
                       "Connect Link, then tap I've connected again.")
            else:
                msg = ("I still do not see an active connection. Tap New link "
                       "to get a fresh Connect Link.")
            telegram.send_message(
                chat_id, msg,
                reply_markup=telegram.connect_keyboard(s.connect_url or ""),
            )
    except Exception as e:  # noqa: BLE001
        log.exception("connection check failed")
        telegram.send_message(chat_id, f"Connection check failed: {e}")


def _after_connect(chat_id: int, user_id: int) -> None:
    s = _state(user_id)
    p = platforms.get(s.platform)
    if not p:
        return
    telegram.send_message(chat_id, f"{p.name} connected.")
    _present_accounts(chat_id, user_id, p,
                      platforms.detect_accounts(user_id, p.slug))


def _present_accounts(chat_id: int, user_id: int, p=None, accounts=None) -> None:
    """Confirm a detected account, ask which one, or ask for the ID."""
    p = p or platforms.get(_state(user_id).platform)
    if not p:
        return
    if accounts is None:
        accounts = platforms.detect_accounts(user_id, p.slug)

    if len(accounts) == 1:
        a = accounts[0]
        _save(user_id, account=a.value, account_label=a.label)
        telegram.send_message(chat_id, f"Posting as: {a.label}")
        _ask_samples(chat_id, user_id)
        return
    if len(accounts) > 1:
        _save(user_id, onboarding_step="account")
        telegram.send_message(
            chat_id,
            "Which account should I post to? Pick one, or send the account "
            "ID/URN.",
            reply_markup=telegram.account_keyboard(accounts),
        )
        return
    if not p.needs_account:
        _ask_samples(chat_id, user_id)
        return
    _save(user_id, onboarding_step="account")
    telegram.send_message(
        chat_id,
        f"Which {p.name} account should I post to?\n\n"
        f"Send the {p.account_hint or 'account ID'}.",
    )


def _accept_account(chat_id: int, user_id: int, value: str,
                    background) -> None:
    s = _state(user_id)
    p = platforms.get(s.platform)
    if not p:
        return
    value = value.strip()
    valid = True
    hint = p.account_hint or "account ID"
    if p.slug == "linkedin":
        valid = value.startswith(("urn:li:person:", "urn:li:organization:"))
        hint = "a person URN (urn:li:person:...) or company URN " \
               "(urn:li:organization:...)"
    elif p.slug == "facebook":
        valid = value.isdigit()
        hint = "a numeric Facebook Page ID"
    elif p.slug == "instagram":
        valid = value.isdigit() or value == "me"
        hint = "a numeric Instagram Business account ID (or 'me')"

    if not valid:
        telegram.send_message(chat_id, f"That does not look like {hint}. Try again.")
        return
    _save(user_id, account=value, account_label=value)
    telegram.send_message(chat_id, f"Posting as: {value}")
    _ask_samples(chat_id, user_id)


# --- voice samples ---------------------------------------------------------

def _ask_samples(chat_id: int, user_id: int) -> None:
    _save(user_id, state="ONBOARDING", onboarding_step="samples")
    n = _count(user_id)
    telegram.send_message(
        chat_id,
        "Step 3 of 6 - Teach me your voice.\n\n"
        f"Paste at least {SAMPLE_TARGET} posts you have already written, one "
        "per message. They can be from any platform. I will learn your tone, "
        "rhythm, hooks, and CTAs.\n\n"
        f"Collected: {n}/{SAMPLE_TARGET}.",
        reply_markup=(telegram.samples_keyboard(n, SAMPLE_TARGET) if n else None),
    )


def _add_sample(chat_id: int, user_id: int, text: str) -> None:
    s = _state(user_id)
    samples = list(s.samples)
    if len(samples) >= 30:
        telegram.send_message(chat_id, "That is plenty of samples. Send /done.")
        return
    samples.append(text.strip()[:6000])
    _save(user_id, samples=samples)
    n = len(samples)
    left = max(0, SAMPLE_TARGET - n)
    if left:
        msg = f"Collected {n}/{SAMPLE_TARGET}. {left} more to go."
    else:
        msg = (f"Collected {n}/{SAMPLE_TARGET}. That is enough - tap Done, "
               "or keep sending more posts.")
    telegram.send_message(
        chat_id, msg,
        reply_markup=telegram.samples_keyboard(n, SAMPLE_TARGET))


def _samples_done(chat_id: int, user_id: int, background) -> None:
    n = _count(user_id)
    if n < SAMPLE_TARGET:
        telegram.send_message(
            chat_id,
            f"I need at least {SAMPLE_TARGET} posts to learn your voice. "
            f"You have {n}. Send {SAMPLE_TARGET - n} more.")
        return
    _save(user_id, onboarding_step="analyzing")
    telegram.send_message(chat_id, "Analysing your voice...")
    background.add_task(_finish_samples, chat_id, user_id)


def _finish_samples(chat_id: int, user_id: int) -> None:
    s = _state(user_id)
    try:
        guide = gemini.analyze_style(s.samples)
    except Exception as e:  # noqa: BLE001
        log.exception("style analysis failed")
        guide = gemini.DEFAULT_STYLE
        telegram.send_message(chat_id, f"Voice analysis failed ({e}); using defaults.")
    _save(user_id, voice_guide=guide)
    telegram.send_message(chat_id, "Voice learned.")
    _ask_mode(chat_id, user_id)


def _ask_mode(chat_id: int, user_id: int) -> None:
    _save(user_id, onboarding_step="mode")
    telegram.send_message(
        chat_id,
        "Step 4 of 6 - How should posts happen?\n\n"
        "Scheduled: I publish drafts at your chosen times.\n"
        "Only when you send material: I draft when you give me something to "
        "post about, and publish after your approval.",
        reply_markup=telegram.mode_keyboard(),
    )


# --- mode + schedule -------------------------------------------------------

def _select_mode(chat_id: int, user_id: int, mode: str, background) -> None:
    if mode not in ("on_demand", "scheduled"):
        telegram.send_message(chat_id, "Pick one of the two options.")
        return
    if mode == "scheduled":
        _save(user_id, posting_mode="scheduled")
        _ask_schedule(chat_id, user_id)
        return
    _save(user_id, posting_mode="on_demand", schedule="",
          onboarding_step="media")
    _ask_media(chat_id, user_id)


def _ask_schedule(chat_id: int, user_id: int) -> None:
    _save(user_id, onboarding_step="schedule")
    telegram.send_message(
        chat_id,
        "Step 5 of 6 - Posting schedule.\n\n"
        "Send a schedule with a timezone, for example:\n"
        "- daily 09:00 Africa/Lagos\n"
        "- weekdays 18:00 Europe/London\n"
        "- mon,wed,fri 08:30 America/New_York",
    )


def _handle_schedule(chat_id: int, user_id: int, text: str, background) -> None:
    from .scheduler import parse_schedule

    try:
        sched = parse_schedule(
            text, default_tz=_state(user_id).timezone or config.DEFAULT_TIMEZONE)
    except ValueError as e:
        telegram.send_message(
            chat_id,
            f"{e}\n\nTry: daily 09:00 Africa/Lagos, weekdays 18:00 "
            "Europe/London, or mon,wed,fri 08:30 America/New_York.")
        return
    _save(user_id, schedule=sched.text, timezone=sched.timezone,
          onboarding_step="media")
    telegram.send_message(chat_id, f"Schedule saved: {sched.text} ({sched.timezone}).")
    _ask_media(chat_id, user_id)


# --- media + finish --------------------------------------------------------

def _ask_media(chat_id: int, user_id: int) -> None:
    _save(user_id, onboarding_step="media")
    telegram.send_message(
        chat_id,
        "Step 6 of 6 - Media.\n\n"
        "What will your posts usually include?",
        reply_markup=telegram.media_keyboard(),
    )


def _select_media(chat_id: int, user_id: int, pref: str) -> None:
    if pref not in ("text", "image", "video", "image_video"):
        telegram.send_message(chat_id, "Pick a media option with the buttons.")
        return
    _save(user_id, media_pref=pref)
    _complete(chat_id, user_id, pref)


def _complete(chat_id: int, user_id: int, pref: str) -> None:
    s = _state(user_id)
    p = platforms.get(s.platform)
    _save(user_id, onboarded=True, onboarding_step="done",
          state="AWAITING_MATERIAL")
    media_copy = {
        "text": "Text. Send ideas, links, or voice notes.",
        "image": ("Images. Send a photo with a caption or idea. For full "
                  "quality, send it as a file."),
        "video": ("Video. Send a Google Drive link (best for long videos), "
                  "upload a video under 20 MB, or send a public URL. Add the "
                  "angle in the caption."),
        "image_video": ("Images and video. Send a photo with a caption; for "
                        "video use a Drive link, a file under 20 MB, or a "
                        "public URL."),
    }[pref]
    ig_note = ("\nInstagram needs a public image/video URL to publish.\n"
               if s.platform == "instagram" else "")
    mode_line = ("Scheduled - " + s.schedule + f" ({s.timezone})"
                 if s.posting_mode == "scheduled" else "Only when you send material")
    telegram.send_message(
        chat_id,
        "You are all set.\n\n"
        f"Platform: {p.name if p else s.platform}\n"
        f"Account: {s.account_label or s.account}\n"
        f"Voice: learned from {len(s.samples)} posts\n"
        f"Mode: {mode_line}\n"
        f"Media: {media_copy}\n{ig_note}\n"
        "Send material whenever you are ready. I will draft it in your voice "
        "and publish after you approve.\n\n"
        "Commands: /status /settings /new",
    )
