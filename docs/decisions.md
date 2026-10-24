# Decisions

- 2026-10-16: LinkedIn target = company page. LINKEDIN_AUTHOR must be organization URN. Composio LinkedIn connection needs org posting scopes. (user)
- 2026-10-16: Video profile = >5min/>200MB. MAX_VIDEO_MB=512, audio-only default ON, MEDIA_RESOLUTION=low. Rationale: token cost + free-tier quota. (user + §9)
- 2026-10-16: Drive access = user OAuth, not service account. Env: GOOGLE_OAUTH_CLIENT_ID/SECRET/REFRESH_TOKEN. Error path "share with SERVICE_ACCOUNT_EMAIL" replaced with "re-connect OAuth / check sharing". Keeps GOOGLE_SERVICE_ACCOUNT_JSON as fallback. (user)
- 2026-10-16: State = Supabase, memory fallback for local dev without keys. Tables: user_state, drafts, processed_updates. (user + §8 ephemeral disk)
- 2026-10-16: Telegram via direct Bot HTTP API (httpx), not aiogram/python-telegram-bot package. Same API, fewer deps, easier webhook + file download. Satisfies §3 purpose.
- 2026-10-16: Supabase via httpx REST, not supabase-py. Same reason.
- 2026-10-16: Crew phases as plain functions (Analyst/Writer/Reviewer-checker, max 3) with Pydantic gates, not CrewAI dependency. Rationale: Karpathy Simplicity + quota (§4: every agent step costs quota). CrewAI can wrap later without changing schemas.
- 2026-10-16: Legacy main.py CLI kept untouched (surgical changes). New entry: uvicorn app.server:app.
- 2026-10-23: Onboarding targets 4 platforms: LinkedIn, X, Facebook Page, Instagram. Per-user Composio sessions + hosted Connect Links (`tg_<telegram_id>`), no custom OAuth. (user)
- 2026-10-23: Minimum 10 past posts as style samples; Gemini builds the voice guide from them; stored as `user_state.profile` JSONB. (user)
- 2026-10-23: Scheduling = in-process daemon thread (SCHEDULER_ENABLED / SCHEDULER_INTERVAL_SEC), no external scheduler; Instagram publishes at the slot (no native scheduling). (design)
- 2026-10-23: Large media staged with Composio `FileUploadable` (`{name,mimetype,s3key}`); base64 data URLs removed. (design)
- 2026-10-23: LinkedIn org-page native video is unsupported by the toolkit → clear error suggesting the personal profile or an image. Instagram needs public media URLs (Telegram files are private). (constraint)
- 2026-10-25: First-time onboarding is a web form linked from the bot (personal signed URL `/onboard?t=…` bound to `telegram_id`, HMAC via `TELEGRAM_WEBHOOK_SECRET`), with the chat flow kept as fallback. Both channels write the same `user_state`/`onboarding_step`, so setup can be mixed freely; drafts + approvals stay in Telegram. (user)
- 2026-10-25: Web wizard needs no new dependencies — f-string HTML with `html.escape`, urlencoded POSTs via `request.form()`, 303-redirect-after-POST, `<meta refresh>` polling while Gemini builds the voice guide (FastAPI background task + in-process guard). (design)
