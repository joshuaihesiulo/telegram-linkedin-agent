"""Gemini wrapper: single generate() with retry + fallback, Files API, phases.

- generate(): PRIMARY → 3x exp-backoff on 500/503 → FALLBACK on 429/404.
- Daily request counter warns at 80% of GEMINI_DAILY_QUOTA (default 1500).
- Files API uploads are reusable across models: upload once, reuse on fallback.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from datetime import date
from pathlib import Path

from . import config
from .schemas import DraftPost, VideoAnalysis

_calls_today: Counter = Counter()
_quota_warned = False
DAILY_QUOTA = int(__import__("os").environ.get("GEMINI_DAILY_QUOTA", "1500") or "1500")


def _bump_counter() -> bool:
    """Returns True if at/above 80% of quota (warn)."""
    global _quota_warned
    key = date.today().isoformat()
    _calls_today[key] += 1
    if _calls_today[key] >= 0.8 * DAILY_QUOTA and not _quota_warned:
        _quota_warned = True
        return True
    return False


def _client(api_key: str | None = None):
    from google import genai

    return genai.Client(api_key=api_key or config.GEMINI_API_KEY)


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc)
    return any(code in msg for code in ("500", "503", "Deadline", "Unavailable"))


def _is_fallbackable(exc: Exception) -> bool:
    msg = str(exc)
    return any(code in msg for code in ("429", "404", "not found", "NotFound"))


def generate(prompt: str, files: list | None = None,
             model: str | None = None) -> tuple[str, bool]:
    """Returns (text, quota_warning). Raises on total failure."""
    from google.genai import types

    models = [model or config.GEMINI_PRIMARY]
    if config.GEMINI_FALLBACK not in models:
        models.append(config.GEMINI_FALLBACK)
    last: Exception | None = None
    for m in models:
        for attempt in range(3):
            try:
                client = _client()
                parts: list = []
                for f in files or []:
                    parts.append(f)
                parts.append(prompt)
                resp = client.models.generate_content(
                    model=m,
                    contents=parts,
                    config=types.GenerateContentConfig(
                        response_mime_type="text/plain",
                    ),
                )
                warn = _bump_counter()
                return resp.text or "", warn
            except Exception as exc:  # noqa: BLE001
                last = exc
                if _is_fallbackable(exc):
                    break  # next model
                if _is_retryable(exc) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                break
    raise RuntimeError(f"Gemini generate() failed on {models}: {last}")


def generate_json(prompt: str, files: list | None = None) -> tuple[dict, bool]:
    from google.genai import types

    models = [config.GEMINI_PRIMARY, config.GEMINI_FALLBACK]
    last: Exception | None = None
    for m in models:
        for attempt in range(3):
            try:
                client = _client()
                parts: list = list(files or []) + [prompt]
                resp = client.models.generate_content(
                    model=m,
                    contents=parts,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                    ),
                )
                warn = _bump_counter()
                return json.loads(resp.text or "{}"), warn
            except Exception as exc:  # noqa: BLE001
                last = exc
                if _is_fallbackable(exc):
                    break
                if _is_retryable(exc) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                break
    raise RuntimeError(f"Gemini generate_json() failed: {last}")


def upload_file(path: Path, mime: str = "video/mp4"):
    """Upload to Files API and poll until ACTIVE. Raises on FAILED."""
    client = _client()
    f = client.files.upload(file=str(path))
    for _ in range(60):
        got = client.files.get(name=f.name)
        state = getattr(got, "state", None)
        state_name = getattr(state, "name", str(state))
        if state_name == "ACTIVE":
            return got
        if state_name == "FAILED":
            raise RuntimeError(f"Gemini Files API state FAILED for {path.name}")
        time.sleep(5)
    raise TimeoutError(f"Gemini file {path.name} not ACTIVE after 5 min")


TRANSCRIBE_PROMPT = "Transcribe this voice note exactly. Return plain text only, no commentary."

ANALYZE_PROMPT = """Watch and listen to this video. Output VALID JSON only with keys:
transcript, key_points (3-5 strings), main_takeaway, on_screen_text (array),
tone, names_numbers_claims (array), topic_tags (array), uncertain_items (array).
Rules: use ONLY what appears in the video or the user instructions below.
Never invent statistics, names, or claims. Put anything unclear in uncertain_items.

User instructions: {instructions}
"""


def transcribe_voice(path: Path) -> str:
    f = upload_file(path, mime="audio/ogg")
    try:
        text, _ = generate(TRANSCRIBE_PROMPT, files=[f])
        return text.strip()
    finally:
        try:
            _client().files.delete(name=f.name)
        except Exception:
            pass


def analyze_video(path: Path, instructions: str) -> VideoAnalysis:
    suffix = path.suffix.lower()
    mime = "audio/mp3" if suffix == ".mp3" else "video/mp4"
    f = upload_file(path, mime=mime)
    # Keep file for potential fallback reuse within this call; delete after.
    try:
        data, _ = generate_json(
            ANALYZE_PROMPT.format(instructions=instructions or "(none)"), files=[f]
        )
        return VideoAnalysis.model_validate(data)
    finally:
        try:
            _client().files.delete(name=f.name)
        except Exception:
            pass


WRITE_PROMPT = """Write a LinkedIn post from this video analysis JSON + user instructions.
Style: {style}
Rules: precise, no filler, no cliches, no emoji spam, no "In today's fast-paced world".
Strong first line, short paragraphs, one clear idea, clear CTA, first person.
Per-request instructions override the default style.
Length: aim 800-1300 chars, HARD MAX 3000 chars including hashtags.
Output VALID JSON only: hook, body, cta, hashtags (3-5, with #), verify_block (names/numbers/claims to double-check).

ANALYSIS: {analysis}
INSTRUCTIONS: {instructions}
"""

DEFAULT_STYLE = (
    "Precise, no filler. Strong first line, short paragraphs, "
    "one clear idea, clear CTA, first person."
)


def write_post(analysis: VideoAnalysis, instructions: str,
               style: str = DEFAULT_STYLE) -> DraftPost:
    data, _ = generate_json(
        WRITE_PROMPT.format(
            style=style, analysis=analysis.model_dump_json(),
            instructions=instructions or "(default style)",
        )
    )
    draft = DraftPost.model_validate(data)
    if draft.char_count() > 3000:
        # Ask for a tightened rewrite once.
        data2, _ = generate_json(
            "Shorten this JSON post to under 3000 chars total, keep meaning: "
            + json.dumps(data)
        )
        draft = DraftPost.model_validate(data2)
    if draft.char_count() > 3000:
        raise ValueError("Draft exceeds LinkedIn 3000-char limit after rewrite")
    return draft
