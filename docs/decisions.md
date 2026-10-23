# Decisions

- 2026-10-16: LinkedIn target = company page. LINKEDIN_AUTHOR must be organization URN. Composio LinkedIn connection needs org posting scopes. (user)
- 2026-10-16: Video profile = >5min/>200MB. MAX_VIDEO_MB=512, audio-only default ON, MEDIA_RESOLUTION=low. Rationale: token cost + free-tier quota. (user + §9)
- 2026-10-16: Drive access = user OAuth, not service account. Env: GOOGLE_OAUTH_CLIENT_ID/SECRET/REFRESH_TOKEN. Error path "share with SERVICE_ACCOUNT_EMAIL" replaced with "re-connect OAuth / check sharing". Keeps GOOGLE_SERVICE_ACCOUNT_JSON as fallback. (user)
- 2026-10-16: State = Supabase, memory fallback for local dev without keys. Tables: user_state, drafts, processed_updates. (user + §8 ephemeral disk)
- 2026-10-16: Telegram via direct Bot HTTP API (httpx), not aiogram/python-telegram-bot package. Same API, fewer deps, easier webhook + file download. Satisfies §3 purpose.
- 2026-10-16: Supabase via httpx REST, not supabase-py. Same reason.
- 2026-10-16: Crew phases as plain functions (Analyst/Writer/Reviewer-checker, max 3) with Pydantic gates, not CrewAI dependency. Rationale: Karpathy Simplicity + quota (§4: every agent step costs quota). CrewAI can wrap later without changing schemas.
- 2026-10-16: Legacy main.py CLI kept untouched (surgical changes). New entry: uvicorn app.server:app.
