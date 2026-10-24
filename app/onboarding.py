"""First-time-user onboarding state machine.

Flow: platform -> Connect Link -> account -> 10 samples -> voice profile ->
posting mode/schedule -> media preference -> live.

This module stays transport-agnostic except for Telegram send helpers, so the
webhook only has to route messages and callbacks here.
"""
from __future__ import annotations

import logging

from . import config, gemini, platforms, telegram, web_tokens
from .store import store

log = logging.getLogger("onboarding")

SAMPLE_TARGET = max(1, config.ONBOARDING_SAMPLES)

# Wizard position per step (used by /start progress notes and the web form).
STEP_NUM = {"platform": 1, "connect": 2, "account": 2, "samples": 3,
            "analyzing": 3, "mode": 4, "schedule": 5, "media": 6, "done": 6}


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


def invite(chat_id: int, user_id: int) -> None:
    """First contact (and /start): offer the web form and the chat flow.

    Progress is never cleared here; /settings is the only reset. Without a
    public base URL this falls back to the original chat-only behaviour.
    """
    s = _state(user_id)
    if s.onboarded:
        return
    url = web_tokens.web_url(user_id)
    if not url:
        if s.state == "ONBOARDING" and s.onboarding_step:
            resume(chat_id, user_id)
        else:
            start(chat_id, user_id, reset=False)
        return
    _save(user_id, invite_sent=True)
    progress = ""
    if s.onboarding_step and s.onboarding_step != "done":
        count = len(s.samples)
        progress = (
            f"You are already part-way: step "
            f"{STEP_NUM.get(s.onboarding_step, 1)} of 6, "
            f"{count} post{'s' if count != 1 else ''} collected. Continue "
            "wherever you like - nothing is lost.\n\n")
    text = (
        "Welcome! I am your social-media posting agent. Setup takes about "
        "10 minutes: platform, account, your voice, posting rhythm, media."
        f"\n\n{progress}"
        "Easiest: the web form - paste many past posts at once and connect "
        "your account in the browser.\n"
        "Or set everything up right here in chat.")
    telegram.send_message(chat_id, text,
                          reply_markup=telegram.invite_keyboard(url))


def handle_callback(chat_id: int, user_id: int, data: str,
                    background) -> bool:
    if not data.startswith("ob:"):
        return False
    parts = data.split(":", 2)
    kind = parts[1] if len(parts) > 1 else ""

    if kind == "noop":
        return True
    if kind == "chat":
        s = _state(user_id)
        if s.state == "ONBOARDING" and s.onboarding_step not in ("", "done"):
            resume(chat_id, user_id)
        else:
            start(chat_id, user_id, reset=False)
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
        telegram.send_message(
            chat_id, "Where do you want to post?",
            reply_markup=telegram.platform_keyboard(platforms.choices()))
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

def set_platform(user_id: int, slug: str) -> bool:
    """Validate + store the chosen platform and move to the connect step."""
    p = platforms.get(slug)
    if not p:
        return False
    _save(user_id, platform=p.slug, account="", account_label="",
          connect_url="", onboarding_step="connect")
    return True


def _select_platform(chat_id: int, user_id: int, slug: str, background) -> None:
    if not set_platform(user_id, slug):
        telegram.send_message(chat_id, "Unknown platform. Pick one below.",
                              reply_markup=telegram.platform_keyboard(
                                  platforms.choices()))
        return
    p = platforms.get(slug)
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


def resolve_accounts(user_id: int, p=None, accounts=None):
    """Decide the account step after connecting.

    Returns (decision, accounts); decision is "auto" (a single account was
    saved), "pick" (several found), "manual" (none found, ID needed), or
    "skip" (this platform needs no account).
    """
    p = p or platforms.get(_state(user_id).platform)
    if not p:
        return "skip", []
    if accounts is None:
        accounts = platforms.detect_accounts(user_id, p.slug)
    if len(accounts) == 1:
        _save(user_id, account=accounts[0].value,
              account_label=accounts[0].label)
        return "auto", accounts
    if len(accounts) > 1:
        return "pick", accounts
    if not p.needs_account:
        return "skip", accounts
    return "manual", accounts


def _present_accounts(chat_id: int, user_id: int, p=None, accounts=None) -> None:
    """Confirm a detected account, ask which one, or ask for the ID."""
    p = p or platforms.get(_state(user_id).platform)
    if not p:
        return
    decision, accounts = resolve_accounts(user_id, p, accounts)
    if decision == "auto":
        telegram.send_message(chat_id, f"Posting as: {accounts[0].label}")
        _ask_samples(chat_id, user_id)
        return
    if decision == "pick":
        _save(user_id, onboarding_step="account")
        telegram.send_message(
            chat_id,
            "Which account should I post to? Pick one, or send the account "
            "ID/URN.",
            reply_markup=telegram.account_keyboard(accounts),
        )
        return
    if decision == "skip":
        _ask_samples(chat_id, user_id)
        return
    _save(user_id, onboarding_step="account")
    telegram.send_message(
        chat_id,
        f"Which {p.name} account should I post to?\n\n"
        f"Send the {p.account_hint or 'account ID'}.",
    )


def validate_account(slug: str, value: str) -> tuple[bool, str]:
    """Check an account id/URN against the platform's format. (ok, hint)."""
    value = (value or "").strip()
    if not value:
        return False, "an account ID"
    if slug == "linkedin":
        ok = value.startswith(("urn:li:person:", "urn:li:organization:"))
        hint = "a person URN (urn:li:person:...) or company URN " \
               "(urn:li:organization:...)"
    elif slug == "facebook":
        ok = value.isdigit()
        hint = "a numeric Facebook Page ID"
    elif slug == "instagram":
        ok = value.isdigit() or value == "me"
        hint = "a numeric Instagram Business account ID (or 'me')"
    else:
        ok = True
        hint = "an account ID"
    return ok, hint


def save_account(user_id: int, value: str) -> tuple[bool, str]:
    """Validate + store a manually entered account. Returns (ok, hint)."""
    s = _state(user_id)
    if not platforms.get(s.platform):
        return False, ""
    ok, hint = validate_account(s.platform, value)
    if ok:
        _save(user_id, account=value.strip(), account_label=value.strip())
    return ok, hint


def _accept_account(chat_id: int, user_id: int, value: str,
                    background) -> None:
    ok, hint = save_account(user_id, value)
    if not ok:
        msg = (f"That does not look like {hint}. Try again." if hint
               else "Pick a platform first with /start.")
        telegram.send_message(chat_id, msg)
        return
    telegram.send_message(chat_id, f"Posting as: {value.strip()}")
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


def set_samples(user_id: int, texts) -> int:
    """Replace the sample set (the web form submits every box at once)."""
    clean: list[str] = []
    for t in texts:
        t = (t or "").strip()
        if t:
            clean.append(t[:6000])
        if len(clean) >= 30:
            break
    _save(user_id, samples=clean)
    return len(clean)


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

def set_mode(user_id: int, mode: str) -> bool:
    """Store the posting mode and move to the schedule or media step."""
    if mode not in ("on_demand", "scheduled"):
        return False
    if mode == "scheduled":
        _save(user_id, posting_mode="scheduled", onboarding_step="schedule")
    else:
        _save(user_id, posting_mode="on_demand", schedule="",
              onboarding_step="media")
    return True


def _select_mode(chat_id: int, user_id: int, mode: str, background) -> None:
    if not set_mode(user_id, mode):
        telegram.send_message(chat_id, "Pick one of the two options.")
        return
    if mode == "scheduled":
        _ask_schedule(chat_id, user_id)
    else:
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


def save_schedule(user_id: int, text: str):
    """Parse + store a schedule string. Raises ValueError with advice."""
    from .scheduler import parse_schedule

    sched = parse_schedule(
        text, default_tz=_state(user_id).timezone or config.DEFAULT_TIMEZONE)
    _save(user_id, schedule=sched.text, timezone=sched.timezone,
          onboarding_step="media")
    return sched


def _handle_schedule(chat_id: int, user_id: int, text: str, background) -> None:
    try:
        sched = save_schedule(user_id, text)
    except ValueError as e:
        telegram.send_message(
            chat_id,
            f"{e}\n\nTry: daily 09:00 Africa/Lagos, weekdays 18:00 "
            "Europe/London, or mon,wed,fri 08:30 America/New_York.")
        return
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
