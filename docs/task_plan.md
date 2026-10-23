# Task Plan — LinkedIn Video-to-Post Agent
Governed by PROJECT CONSTITUTION. All code is Python.

## Decisions baked in (user answers 2026-10-16)
- LinkedIn: Company page → LINKEDIN_AUTHOR = organization URN
- Video: >5 min / >200MB → MAX_VIDEO_MB=512, default audio-only + MEDIA_RESOLUTION=low
- Drive: My OAuth (not service account) → GOOGLE_OAUTH_* env, no re-share flow
- State: Supabase (not Redis) → SUPABASE_URL/SUPABASE_KEY, memory fallback for local dev

## Milestone 1 — Smoke tests
- [ ] Telegram echo bot (webhook + long-poll fallback)
- [ ] Drive OAuth download by file ID (real file)
- [ ] Gemini video/audio analysis on one real >5min video
- [ ] Composio LinkedIn TEXT post as org (dry-run first)
- [ ] Composio LinkedIn VIDEO post as org
- Record exact Composio action names in docs/findings.md. Do not hardcode from memory.

## Milestone 2 — State machine + link flow (no AI)
- [ ] Supabase tables: user_state, drafts, processed_updates (SQL in docs/findings.md)
- [ ] States: IDLE → AWAITING_LINK → PROCESSING → AWAITING_APPROVAL → PUBLISHED (+EDITING/REJECTED/EXPIRED/FAILED)
- [ ] Drive URL parsing (/file/d/ID, open?id=ID, uc?id=ID)
- [ ] Invalid-link and OAuth-permission error messages
- [ ] Commands: /start /new /cancel /status
- [ ] Webhook secret check, ALLOWED_TELEGRAM_USER_ID gate, update_id dedupe
- [ ] Background task (never block webhook)

## Milestone 3 — Analyze + Write
- [ ] Pydantic schemas between every phase (app/schemas.py)
- [ ] generate() wrapper: PRIMARY → retry 500/503 x3 exp backoff → FALLBACK on 429/404
- [ ] Files API upload + poll ACTIVE, handle FAILED
- [ ] Audio-only default via ffmpeg for >5min videos
- [ ] Analysis JSON: transcript, key_points[3-5], main_takeaway, on_screen_text, tone, names_numbers_claims, topic_tags, uncertain_items
- [ ] Write: hook, body 800-1300 chars (max 3000), CTA, 3-5 hashtags, verify block
- [ ] Style calibration: need 2-3 past posts from user (pending)

## Milestone 4 — Approve / Edit / Reject
- [ ] Draft + Drive link + Verify block + [Approve][Edit][Reject] inline buttons
- [ ] Edit re-runs Write on stored analysis (no re-analysis)
- [ ] Reject → IDLE, expiry 24h, expired/double-approve errors
- [ ] Idempotent publish (double-tap safe)

## Milestone 5 — Publish with video
- [ ] Re-download from Drive (temp disk ephemeral)
- [ ] Composio native video post, capture post URL, reply to Telegram
- [ ] Delete temp files on success AND failure

## Milestone 6 — Deploy
- [ ] Dockerfile + requirements.txt, /health, uvicorn
- [ ] Railway/Render, PUBLIC_BASE_URL, setWebhook with secret
- [ ] Structured logging, no secret/content logging, user-facing error messages

## Milestone 7 — Acceptance tests
- Voice → ask link → valid link → draft + buttons
- Invalid/unshared link guidance, state intact
- Quota fallback transparent
- Edit without re-analysis
- Double-Approve posts once
- Expired draft cannot approve
- Other Telegram users ignored
- Temp files removed on success + failure
