"""Telegram Bot HTTP API helpers (direct, via httpx)."""
from __future__ import annotations

from pathlib import Path

import httpx

from . import config


def _base() -> str:
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")
    return f"{config.TELEGRAM_API}/bot{config.TELEGRAM_BOT_TOKEN}"


def send_message(chat_id: int, text: str,
                 reply_markup: dict | None = None) -> dict:
    r = httpx.post(
        f"{_base()}/sendMessage",
        json={"chat_id": chat_id, "text": text,
              "reply_markup": reply_markup} if reply_markup
        else {"chat_id": chat_id, "text": text},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def answer_callback(callback_id: str, text: str = "") -> None:
    try:
        httpx.post(f"{_base()}/answerCallbackQuery",
                   json={"callback_query_id": callback_id, "text": text},
                   timeout=10)
    except Exception:
        pass


def approval_keyboard(draft_id: str, can_schedule: bool = False) -> dict:
    row = [
        {"text": "Approve" if not can_schedule else "Post now",
         "callback_data": f"approve:{draft_id}"},
        {"text": "Edit", "callback_data": f"edit:{draft_id}"},
        {"text": "Reject", "callback_data": f"reject:{draft_id}"},
    ]
    rows = [row]
    if can_schedule:
        rows.insert(0, [{"text": "Schedule next slot",
                         "callback_data": f"sched:{draft_id}"}])
    return {"inline_keyboard": rows}


def skip_keyboard() -> dict:
    return {"inline_keyboard": [[
        {"text": "Skip", "callback_data": "skip_instructions"},
    ]]}


def platform_keyboard(platforms) -> dict:
    buttons = [{"text": p.name, "callback_data": f"ob:platform:{p.slug}"}
               for p in platforms]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return {"inline_keyboard": rows}


def connect_keyboard(url: str) -> dict:
    rows = []
    if url:
        rows.append([{"text": "Connect account", "url": url}])
    rows.append([
        {"text": "I've connected", "callback_data": "ob:connected"},
        {"text": "New link", "callback_data": "ob:newlink"},
    ])
    return {"inline_keyboard": rows}


def account_keyboard(accounts) -> dict:
    return {"inline_keyboard": [
        [{"text": a.label[:60], "callback_data": f"ob:acct:{a.value}"}]
        for a in accounts[:8]
    ]}


def invite_keyboard(web_url: str = "") -> dict:
    """First-contact choices: open the web form, or set up in chat."""
    if web_url:
        return {"inline_keyboard": [
            [{"text": "Open web setup", "url": web_url}],
            [{"text": "Do it here instead", "callback_data": "ob:chat"}],
        ]}
    return {"inline_keyboard": [
        [{"text": "Set up here", "callback_data": "ob:chat"}],
    ]}


def samples_keyboard(count: int, target: int) -> dict:
    if count < target:
        return {"inline_keyboard": [[
            {"text": f"{count}/{target} collected", "callback_data": "ob:noop"},
        ]]}
    return {"inline_keyboard": [[
        {"text": "Done - learn my voice", "callback_data": "ob:samples_done"},
    ]]}


def mode_keyboard() -> dict:
    return {"inline_keyboard": [[
        {"text": "Scheduled", "callback_data": "ob:mode:scheduled"},
        {"text": "Only when I send material", "callback_data": "ob:mode:on_demand"},
    ]]}


def media_keyboard() -> dict:
    return {"inline_keyboard": [
        [{"text": "Text only", "callback_data": "ob:media:text"},
         {"text": "Images", "callback_data": "ob:media:image"}],
        [{"text": "Video", "callback_data": "ob:media:video"},
         {"text": "Images + video", "callback_data": "ob:media:image_video"}],
    ]}


def download_file(file_path: str, dest: Path) -> Path:
    """Download a Telegram file (voice notes are small, <20MB)."""
    url = f"{config.TELEGRAM_API}/file/bot{config.TELEGRAM_BOT_TOKEN}/{file_path}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(1024 * 64):
                f.write(chunk)
    return dest


def get_file(file_id: str) -> str:
    return get_file_info(file_id)["file_path"]


def get_file_info(file_id: str) -> dict:
    r = httpx.post(f"{_base()}/getFile", json={"file_id": file_id}, timeout=20)
    r.raise_for_status()
    return r.json()["result"]
