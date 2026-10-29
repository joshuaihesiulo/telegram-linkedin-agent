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
# Create the venv OUTSIDE OneDrive: a synced site-packages folder makes every
# import crawl (minutes instead of seconds).
py -3.12 -m venv $env:USERPROFILE\venvs\tla
$PY = "$env:USERPROFILE\venvs\tla\Scripts\python.exe"
& $PY -m pip install -r requirements.txt
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
& $PY main.py
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
& $PY show_mcp.py
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
& $PY -m pytest tests/ -q
& $PY -m uvicorn app.server:app --port 8000 --env-file .env
& $PY scripts/set_webhook.py  # after deploy, sets PUBLIC_BASE_URL/webhook
```

The app reads keys from the environment only (no dotenv import), so pass
`--env-file .env`. For a local web-setup demo set
`PUBLIC_BASE_URL=http://localhost:8000` in `.env`; in production it must be
the public HTTPS URL of this server (webhook + personal setup links).

For a live Telegram test without deploying, use a Cloudflare quick tunnel and
point both the server and the webhook at it:

```powershell
cloudflared tunnel --url http://127.0.0.1:8000   # prints https://<random>.trycloudflare.com
$env:PUBLIC_BASE_URL = "https://<random>.trycloudflare.com"
& $PY -m uvicorn app.server:app --port 8000 --env-file .env
& $PY scripts/set_webhook.py
```

The quick-tunnel hostname changes on every run, so re-run `set_webhook.py`
each time (and set `PUBLIC_BASE_URL` before starting the server — invite links
are built from it).

## First-time onboarding

`/start` (or any first message) offers two ways to set up — a web form
(easiest for pasting many posts) or the same flow entirely in chat. Both
write the same saved progress, so you can switch mid-way without losing
anything; `/settings` is the only reset.

Web form: the bot sends a personal signed link
(`PUBLIC_BASE_URL/onboard?t=…`, bound to your Telegram id — no login). The
wizard walks through:

1. Platform (LinkedIn, X, Facebook Page, Instagram).
2. Connect Link (Composio hosted sign-in), then confirm the account it found
   or type the id/URN.
3. At least 10 past posts so Gemini can learn your voice; the page polls
   while the voice guide is built.
4. Scheduled posting (days, time, timezone) or on-demand.
5. Media preference, with video instructions.

The bot confirms in Telegram when the profile is live; drafting and approval
stay in chat.

In chat:

1. `/start` → "Do it here instead" → pick the platform.
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
- Web onboarding (`app/web_ui.py`) writes the same `user_state`/`onboarding_step`
  the chat flow uses; links are HMAC-signed with `TELEGRAM_WEBHOOK_SECRET`
  (`app/web_tokens.py`). No extra dependencies — f-string HTML + urlencoded forms.
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
