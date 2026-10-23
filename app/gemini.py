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


WRITE_PROMPT = """Write a {platform} post from this source analysis JSON + user instructions.
Voice/style profile: {style}
{samples}
Rules: precise, no filler, no cliches, no emoji spam, no "In today's fast-paced world".
Strong first line, short paragraphs, one clear idea, clear CTA, first person.
Per-request instructions override the default style.
Length: HARD MAX {max_chars} chars including hashtags.
Output VALID JSON only: hook, body, cta, hashtags (3-5, with #),
verify_block (names/numbers/claims to double-check).

ANALYSIS: {analysis}
INSTRUCTIONS: {instructions}
"""

MATERIAL_PROMPT = """Write a {platform} post from this material.
Voice/style profile: {style}
{samples}
Rules: mirror the writer's voice, precise, no filler, no cliches, no emoji spam.
Strong first line, short paragraphs, one clear idea, clear CTA, first person.
Use only facts present in the material or instructions; invent nothing.
Length: HARD MAX {max_chars} chars including hashtags.
Output VALID JSON only: hook, body, cta, hashtags (3-5, with #),
verify_block (names/numbers/claims to double-check).

MATERIAL:
{material}
"""

STYLE_PROMPT = """You are calibrating a social-media ghostwriter.

Below are {count} past posts by the same person. Read them and infer a reusable
voice guide. Return VALID JSON only with keys:
- voice_guide: 4-8 sentences in imperative form covering tone, rhythm, sentence
  length, vocabulary, openness, humour, formatting, hook style, CTA style, and
  topics they naturally write about.
- traits: array of 3-6 short labels.
- avoid: array of 2-5 things this writer never does.
Be specific and evidence-based; do not invent biography.

POSTS:
{samples}
"""

DEFAULT_STYLE = (
    "Precise, no filler. Strong first line, short paragraphs, "
    "one clear idea, clear CTA, first person."
)


def _sample_excerpts(samples: list[str], limit: int = 3) -> str:
    if not samples:
        return ""
    excerpts = [s.strip()[:600] for s in samples[-limit:] if s.strip()]
    if not excerpts:
        return ""
    joined = "\n---\n".join(excerpts)
    return f"Representative past posts (match this voice):\n{joined}\n"


def analyze_style(samples: list[str]) -> str:
    """Turn past posts into a voice guide. Falls back to a safe default."""
    if not samples:
        return DEFAULT_STYLE
    prompt = STYLE_PROMPT.format(
        count=len(samples),
        samples="\n---\n".join(s.strip() for s in samples if s.strip())[:24000],
    )
    try:
        data, _ = generate_json(prompt)
        guide = str(data.get("voice_guide", "")).strip()
        if not guide:
            raise ValueError("empty voice_guide")
        traits = [str(t) for t in data.get("traits", []) if str(t).strip()]
        avoid = [str(t) for t in data.get("avoid", []) if str(t).strip()]
        parts = [guide]
        if traits:
            parts.append("Traits: " + ", ".join(traits[:6]) + ".")
        if avoid:
            parts.append("Avoid: " + "; ".join(avoid[:5]) + ".")
        return " ".join(parts)
    except Exception as e:  # noqa: BLE001
        log_msg = f"style analysis failed, using fallback: {e}"
        import logging

        logging.getLogger("gemini").warning(log_msg)
        return DEFAULT_STYLE


def write_post(analysis: VideoAnalysis, instructions: str,
               style: str = DEFAULT_STYLE, platform: str = "LinkedIn",
               max_chars: int = 3000,
               samples: list[str] | None = None) -> DraftPost:
    data, _ = generate_json(
        WRITE_PROMPT.format(
            platform=platform, style=style, analysis=analysis.model_dump_json(),
            instructions=instructions or "(default style)",
            max_chars=max_chars, samples=_sample_excerpts(samples or []),
        )
    )
    draft = DraftPost.model_validate(data)
    if draft.char_count() > max_chars:
        # Ask for a tightened rewrite once.
        data2, _ = generate_json(
            f"Shorten this JSON post to under {max_chars} chars total, "
            "keep meaning: " + json.dumps(data)
        )
        draft = DraftPost.model_validate(data2)
    if draft.char_count() > max_chars:
        raise ValueError(
            f"Draft exceeds {platform} {max_chars}-char limit after rewrite")
    return draft


def write_material_post(material: str, style: str = DEFAULT_STYLE,
                        platform: str = "LinkedIn", max_chars: int = 3000,
                        samples: list[str] | None = None) -> DraftPost:
    """Draft from a text idea, caption, transcript or link (no video JSON)."""
    data, _ = generate_json(
        MATERIAL_PROMPT.format(
            platform=platform, style=style, material=material[:12000],
            max_chars=max_chars, samples=_sample_excerpts(samples or []),
        )
    )
    draft = DraftPost.model_validate(data)
    if draft.char_count() > max_chars:
        data2, _ = generate_json(
            f"Shorten this JSON post to under {max_chars} chars total, "
            "keep meaning: " + json.dumps(data)
        )
        draft = DraftPost.model_validate(data2)
    if draft.char_count() > max_chars:
        raise ValueError(
            f"Draft exceeds {platform} {max_chars}-char limit after rewrite")
    return draft
