# Findings

## Runtime verification (2026-10-16, global python 3.14)
- fastapi 0.111.0, uvicorn 0.30.1, pydantic 2.13.5, httpx 0.28.1 — present
- google-genai 2.30.1, google-api-python-client 2.201.0, google-auth 2.60.0 — present
- composio_core 0.3.30 (legacy) present; v3 `composio==0.26.0` from requirements.txt NOT installed in venv (venv empty). Code supports both, prefers v3 if available.
- No supabase / aiogram packages — using httpx REST for Supabase and direct Bot HTTP API (avoids extra deps, same API surface).
- ffmpeg: not verified yet — audio extraction is optional; pipeline falls back to full-file upload if ffmpeg missing.

## Composio LinkedIn actions (DO NOT hardcode — discover at runtime)
Discovery script: `venv\Scripts\python.exe scripts/discover_composio.py` (tries v3 then legacy).
Status 2026-10-16: discovery attempted on global python 3.14 — FAILED as expected:
- `from composio import Composio` raises AttributeError in legacy composio 0.3.x
  (`Action.__annotations__` broken on 3.14).
- `pip show composio` = not found (only legacy composio_core 0.3.30 present).
Next: install `composio==0.26.0` (requirements.txt) in a Python 3.12 venv/Docker,
set COMPOSIO_API_KEY + COMPOSIO_ENTITY_ID, re-run discovery, record exact
LINKEDIN_* action names here before Milestone 5. app/linkedin.py tries
LINKEDIN_CREATE_VIDEO_POST then LINKEDIN_CREATE_POST and surfaces a clear
operator error until discovery succeeds.

## Supabase schema (run in SQL editor)
```sql
create table if not exists user_state (
  telegram_id bigint primary key,
  state text not null default 'IDLE',
  instructions text default '',
  drive_file_id text default '',
  updated_at timestamptz default now()
);
create table if not exists drafts (
  draft_id text primary key,
  telegram_id bigint not null,
  drive_file_id text not null,
  analysis_json jsonb not null,
  draft_text text not null,
  status text not null default 'pending',
  created_at timestamptz default now(),
  expires_at timestamptz not null,
  post_url text default ''
);
create table if not exists processed_updates (
  update_id bigint primary key,
  created_at timestamptz default now()
);
```

## Gemini models
- GEMINI_PRIMARY default: gemini-2.5-flash
- GEMINI_FALLBACK default: gemini-3-flash-preview — VERIFY exact ID in AI Studio before Milestone 3 live run (preview IDs change).
- Files API files are reusable across models — upload once, reuse on fallback.
- Large video token note: ~260+ tokens/sec at default resolution → default MEDIA_RESOLUTION=low + audio-only for >5min.

## Default style profile (needs calibration with 2-3 past posts)
- Precise, no filler, no clichés, no emoji spam, no "In today's fast-paced world".
- Strong first line, short paragraphs, one clear idea, clear CTA.
- First person. Per-request voice/text overrides win over this default.
- Target 800-1300 chars, hard max 3000.
