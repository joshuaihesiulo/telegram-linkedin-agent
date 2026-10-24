# Progress

## 2026-10-16 — Init + user answers
- Read constitution + workspace (main.py CLI prototype, no docs/, no app/, venv empty, global python has fastapi/uvicorn/pydantic/httpx/google-genai).
- Asked §11 questions. Answers: company page / >5min+200MB / My OAuth / Supabase.
- Created docs/task_plan.md, findings.md, progress.md, decisions.md.

## 2026-10-16 — Implementation (done, pending live keys)
- Scaffold app/: config, schemas, store (Supabase REST + memory fallback),
  drive (OAuth + file-ID parse + streamed download + ffmpeg audio),
  gemini (generate fallback + Files API + transcribe/analyze/write),
  telegram (direct Bot API), linkedin (Composio discovery + publish),
  pipeline (INGEST→ANALYZE→WRITE→REVIEW + rewrite), server (webhook state machine).
- Added Dockerfile (py3.12 + ffmpeg), requirements.txt, scripts/discover_composio.py,
  scripts/set_webhook.py, tests (17 passed on global py3.14).
- Verified: compileall ok, Drive-ID parse (4 formats), Pydantic gates,
  dedupe/expiry, other-user ignore, invalid-link guidance, double-Approve posts once.
- Next (needs .env + real video): Drive OAuth download, Gemini live analysis,
  Composio discovery + org video post, style calibration with 2-3 past posts.

## 2026-10-23 — Onboarding + multi-platform publishing
- Onboarding state machine (`app/onboarding.py`): platform → Connect Link →
  account detection/entry → ≥10 samples → Gemini voice guide → scheduled vs
  on-demand → schedule parsing → media preference → live.
- New modules: `composio_client` (per-user sessions, Connect Links, `execute`,
  `upload_file`), `platforms` (registry + publish adapters for LinkedIn / X /
  Facebook Page / Instagram), `scheduler` (in-process due-draft daemon),
  `publisher` (media download + publish).
- Verified Composio 0.30.0 contracts from live docs (recorded in findings):
  LinkedIn two-step video (member-owned; clear org-page error), X large-media
  upload + status poll, Facebook page posts, Instagram's new
  `INSTAGRAM_POST_IG_USER_MEDIA[_PUBLISH]` actions.
- Media staged with `FileUploadable` (streams to S3; base64 dropped). Drafts
  carry platform/media/scheduling; `user_state.profile` JSONB stores the
  onboarding profile (migration SQL in findings).
- Fixed: Composio result unwrapping (`.data` / JSON-string payloads), empty
  Connect-Link keyboard, Instagram uploaded-image → public-URL handoff,
  first-time non-`/start` message no longer double-prompts.
- Tests: 48 passed (11 new platform adapter tests; onboarding / material /
  store / scheduler / server coverage).
- Next (needs live keys + Composio installed): end-to-end Connect Link, sample
  voice-analysis quality, and one real publish per platform.

## 2026-10-25 — Web onboarding + invite flow
- Fixed `/start` clearing onboarding progress mid-flow (now resumes; only
  `/settings` resets). Committed separately as `6288088`.
- `/start` (and first contact) offers both routes: a personal signed web link
  (`app/web_tokens.py`, HMAC with the webhook secret, 30-day TTL) and
  "Do it here instead" (`ob:chat`); a second plain message auto-starts the
  chat flow (`invite_sent`). Without `PUBLIC_BASE_URL` it falls back to the
  original chat-only behaviour.
- New `app/web_ui.py`: six-step wizard (platform → Connect Link → account →
  ≥10 posts → mode → schedule → media) on the FastAPI app, PRG redirects,
  meta-refresh polling while Gemini learns the voice (background task +
  in-process guard), done page hands back to Telegram, which receives the
  "You are all set" message. Great for pasting many posts at once.
- Onboarding internals split into channel-agnostic helpers (`set_platform`,
  `save_account`, `resolve_accounts`, `set_samples`, `set_mode`,
  `save_schedule`) so web and chat stay interchangeable mid-flow.
- Tests: 57 passed (+7 web onboarding: token sign/tamper/expiry, invalid-link
  400, full web flow with Telegram handoff, schedule validation renders,
  first-contact invite linking, web-aware /start + resume, account picker +
  analyzing page).
- Next: live end-to-end run needs `GEMINI_API_KEY` + `COMPOSIO_API_KEY` in
  `.env` and a public HTTPS URL (deploy or tunnel) for `scripts/set_webhook.py`.
