# Telegram AI Agent (Gemini + Composio)

Connects Telegram to a Gemini agent through Composio. Two routes, same
Composio project:

| Route | File | Needs |
|---|---|---|
| Native SDK agent (recommended) | `main.py` | `google-genai` + `composio_gemini` (installed) |
| MCP endpoint for any MCP client | `show_mcp.py` / `mcp_config.json` | No extra package |

> Windows note: the Composio CLI installer only supports Linux/macOS
> (Windows needs WSL), so this project uses the Python SDK + hosted MCP
> instead of the CLI. Nothing else to install.

## Setup

```powershell
cd C:\Users\joshu\OneDrive\Documents\Projects\telegram-linkedin-agent
py -3.12 -m venv venv   # already created; skip if venv exists
venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env  # then fill in the keys
```

Keys:

- `COMPOSIO_API_KEY` - from the Composio dashboard
  (project Settings -> API keys).
- `GOOGLE_API_KEY` - from Google AI Studio.
- `TELEGRAM_USER_ID` - stable id for the end user (each id keeps its
  own Telegram connection).

## Run the agent

```powershell
venv\Scripts\python.exe main.py
```

Requires a Telegram **bot token** - message @BotFather on Telegram,
create a bot, and connect the token in Composio. On first use the agent
gives you a Connect Link; open it, paste the bot token, then continue.

Telegram rules to know: bots cannot DM users who haven't messaged the
bot first, and `chat_id` values come from `TELEGRAM_GET_UPDATES`. Rate
limit is roughly 1 message/second per chat.

## Use over MCP (Cursor / Claude / VS Code / ...)

Option A - per-session endpoint scoped to Telegram only:

```powershell
venv\Scripts\python.exe show_mcp.py
```

Point your MCP client at the printed URL + headers.

Option B - shared Composio Connect URL for all toolkits:

1. Get a consumer key from the Composio dashboard (AI Clients setup).
2. Put it in `mcp_config.json` as `x-consumer-api-key`.
3. Add the server to your client, e.g. VS Code / Cursor MCP config.

## Files

- `main.py` - interactive Gemini agent with Telegram tools.
- `show_mcp.py` - prints a Telegram-scoped session MCP URL + headers.
- `mcp_config.json` - template for the hosted Composio Connect MCP server.
- `requirements.txt` - pinned Python dependencies (no legacy
  `composio-core`; the current package is `composio` v3 sessions API).

---

# Social Posting Agent (Telegram + Gemini + Composio)

Telegram bot → first-time onboarding → drafts in your voice → Approve/Edit/Reject
→ publish to LinkedIn, X, a Facebook Page, or Instagram. Approval (or the
scheduler) is the only publishing path.

See `docs/task_plan.md`, `docs/findings.md`, `docs/progress.md`, `docs/decisions.md`.

## Run the webhook server

```powershell
copy .env.example .env  # fill in keys (Telegram, Gemini, Composio, Supabase, Drive)
python -m pytest tests/ -q
python -m uvicorn app.server:app --port 8000
python scripts/set_webhook.py  # after deploy, sets PUBLIC_BASE_URL/webhook
```

## First-time onboarding

1. `/start` → pick the platform (LinkedIn, X, Facebook Page, Instagram).
2. Tap the Connect Link and grant access; the bot verifies the connection and
   reads back the account it found (person/page/IG account). You can also type
   an account id (e.g. the LinkedIn person or organization URN).
3. Send at least 10 past posts (text or voice notes) so Gemini can learn your
   voice; the bot builds a style guide from them.
4. Choose scheduled posting (e.g. `weekdays 09:00 Africa/Lagos`) or on-demand.
5. Choose how images are handled. Video: send the file to the bot (<20 MB) or
   give a public URL / Drive link, or it will ask for one before drafting.

After that, send an idea, photo, video, voice note, or Drive link and the bot
drafts a post for approval. Commands: `/start /settings /status /new /cancel /help`.

## Platform support

| Platform | Text | Image | Video | Notes |
|---|---|---|---|---|
| LinkedIn | yes | yes | personal profile only | org-page native video is not exposed by the toolkit — attach an image or post text instead |
| X | yes | yes | yes | video/large images use the chunked upload endpoint and wait for processing |
| Facebook Page | yes | yes | yes | pick a page from the managed-pages list during onboarding |
| Instagram | caption | yes | yes | Business/Creator accounts only; needs a public image/video URL (Telegram files are private), so the bot asks you for one |

## How it works

- Media is staged with Composio's `FileUploadable` (streamed to S3 — no base64
  in memory), then published through the user's own Composio session.
- Scheduling is an in-process daemon thread (`SCHEDULER_ENABLED`,
  `SCHEDULER_INTERVAL_SEC`); `DEFAULT_TIMEZONE` seeds onboarding and
  `ONBOARDING_SAMPLES` sets the minimum sample count (default 10).
- Legacy path: voice/text instructions → bot asks for a Drive link → pipeline
  runs in background (Downloading → Analyzing → Writing) → draft + Verify block
  + [Approve][Edit][Reject] → Approve publishes via Composio, replies post URL.

Notes:
- Drive auth is user OAuth (GOOGLE_OAUTH_*). Service-account JSON optional fallback.
- State in Supabase (`user_state`, `drafts`, `processed_updates`); run the
  migration SQL in `docs/findings.md` before upgrading an existing deployment
  (adds `user_state.profile` and the draft media/scheduling columns). Memory
  fallback is for local dev only.
- Large videos default to ffmpeg audio-only + low resolution (MAX_VIDEO_MB=512).
- Composio action names, fields, and SDK signatures are recorded in
  `docs/findings.md`; refresh with `scripts/discover_composio.py` when the SDK
  or a toolkit changes.
