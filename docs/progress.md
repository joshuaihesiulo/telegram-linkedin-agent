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
