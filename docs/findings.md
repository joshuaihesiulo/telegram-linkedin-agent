# Findings

## Runtime verification (2026-10-16, global python 3.14)
- fastapi 0.111.0, uvicorn 0.30.1, pydantic 2.13.5, httpx 0.28.1 — present
- google-genai 2.30.1, google-api-python-client 2.201.0, google-auth 2.60.0 — present
- composio_core 0.3.30 (legacy) may still be present; the project pins v3 `composio==0.30.0` (see below). Dev machine: composio not installed — imports are lazy, tests monkeypatch the module.
- No supabase / aiogram packages — using httpx REST for Supabase and direct Bot HTTP API (avoids extra deps, same API surface).
- ffmpeg: not verified yet — audio extraction is optional; pipeline falls back to full-file upload if ffmpeg missing.

## Composio SDK 0.30.0 (verified 2026-10-23 against the live toolkit docs)

Pinned in requirements.txt: `composio==0.30.0`, `tzdata==2026.5` (ZoneInfo on
Windows). Dev machines may not have `composio` installed; imports are lazy and
tests monkeypatch the module.

Session / per-user API (app/composio_client.py):
- `Composio(api_key=...)`; `client.sessions.create(user_id="tg_<telegram_id>",
  toolkits=[TOOLKIT])` — one stable user key per Telegram user isolates connections.
- Connect Link: `session.authorize(toolkit)` -> hosted `redirect_url` (no custom OAuth).
- Connection check: `client.connected_accounts.list(user_id=..., toolkit_slugs=[...])`.
- Execute: `session.execute(ACTION, arguments={...})` — `arguments` is KEYWORD-ONLY.
- Legacy entity path (pre-onboarding drafts only):
  `client.tools.execute(ACTION, {...}, user_id=config.COMPOSIO_ENTITY_ID)`.

File staging (large media, no base64):
- `session.execute` does NOT auto-upload local paths.
- `FileUploadable.from_path(client=client.client, file=Path, tool=ACTION,
  toolkit=TOOLKIT).model_dump()` -> `{name, mimetype, s3key}`; pass that dict
  as the file argument value. Streams to S3/R2, so large videos are fine.
- Instagram cannot fetch private files; it takes public URLs only.

Verified action names + fields (docs.composio.dev/toolkits/<name>.md — never
hardcode from memory; use scripts/discover_composio.py to refresh):
- LinkedIn:
  - `LINKEDIN_UPLOAD_VIDEO` (`file` | `video_url`; waits until ready) ->
    `LINKEDIN_CREATE_VIDEO_POST` (`video_urn`, `commentary`, `visibility`).
    Member-owned (personal profile) ONLY — org pages are not supported.
  - `LINKEDIN_INITIALIZE_IMAGE_UPLOAD` (`owner`) -> PUT bytes to `upload_url` ->
    `LINKEDIN_REGISTER_IMAGE_UPLOAD` (`owner_urn`) -> image URN ->
    `LINKEDIN_CREATE_LINKED_IN_POST` (`author`, `commentary`, `images: [urn]`).
- X: `TWITTER_UPLOAD_MEDIA` (images <= ~5MB) | `TWITTER_UPLOAD_LARGE_MEDIA`
  (video/GIF/>5MB), both (`media`, `media_category`) -> poll
  `TWITTER_GET_MEDIA_UPLOAD_STATUS` (`media_id`) until state=succeeded ->
  `TWITTER_CREATION_OF_A_POST` (`text`, `media_media_ids`).
- Facebook Page: `FACEBOOK_CREATE_POST` (`page_id`, `message`) |
  `FACEBOOK_CREATE_PHOTO_POST` (`photo`) | `FACEBOOK_CREATE_VIDEO_POST`
  (`video`, `description`); pages come from `FACEBOOK_LIST_MANAGED_PAGES`.
- Instagram (Business/Creator only): `INSTAGRAM_GET_USER_INFO`
  (`ig_user_id="me"`) -> `INSTAGRAM_POST_IG_USER_MEDIA`
  (`image_url` | `video_url`, `caption`, `ig_user_id`) ->
  `INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH` (`creation_id`, `max_wait_seconds`;
  polls until FINISHED). `INSTAGRAM_CREATE_MEDIA_CONTAINER` and
  `INSTAGRAM_CREATE_POST` are DEPRECATED.

## Supabase schema (run in SQL editor)
Base tables (fresh deploy):
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
  analysis_json jsonb,          -- nullable: material posts have no video analysis
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

Onboarding + multi-platform migration (existing deployments):
```sql
alter table user_state add column if not exists profile jsonb default '{}'::jsonb;
alter table drafts alter column analysis_json drop not null;
alter table drafts add column if not exists platform text default '';
alter table drafts add column if not exists media_type text default 'text';
alter table drafts add column if not exists media_file_id text default '';
alter table drafts add column if not exists media_url text default '';
alter table drafts add column if not exists scheduled_for timestamptz;
```
`profile` stores the whole onboarding object (platform, account, samples,
voice_guide, posting_mode, schedule, timezone, media_pref, pending_*). Without
it, writes fall back to memory and onboarding is lost on restart.

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
