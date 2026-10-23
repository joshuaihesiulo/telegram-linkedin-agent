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


def approval_keyboard(draft_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "Approve", "callback_data": f"approve:{draft_id}"},
            {"text": "Edit", "callback_data": f"edit:{draft_id}"},
            {"text": "Reject", "callback_data": f"reject:{draft_id}"},
        ]]
    }


def skip_keyboard() -> dict:
    return {"inline_keyboard": [[
        {"text": "Skip", "callback_data": "skip_instructions"},
    ]]}


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
    r = httpx.post(f"{_base()}/getFile", json={"file_id": file_id}, timeout=20)
    r.raise_for_status()
    return r.json()["result"]["file_path"]
