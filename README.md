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

# LinkedIn Video-to-Post Agent (new, constitution-governed)

Telegram bot → Drive download → Gemini watch/listen → LinkedIn draft →
Approve/Edit/Reject → native video post. Approval is the only publishing path.

See `docs/task_plan.md`, `docs/findings.md`, `docs/progress.md`, `docs/decisions.md`.

## Run the webhook server

```powershell
copy .env.example .env  # fill in keys (OAuth, Supabase, Telegram, Composio)
python -m pytest tests/ -q
python -m uvicorn app.server:app --port 8000
python scripts/set_webhook.py  # after deploy, sets PUBLIC_BASE_URL/webhook
```

Flow: send voice/text instructions → bot asks for Drive link → pipeline runs
in background (Downloading → Analyzing → Writing) → draft + Verify block +
[Approve][Edit][Reject] → Approve publishes via Composio, replies post URL.

Notes:
- Drive auth is user OAuth (GOOGLE_OAUTH_*). Service-account JSON optional fallback.
- State in Supabase (`user_state`, `drafts`, `processed_updates` SQL in findings);
  memory fallback for local dev only.
- Large videos default to ffmpeg audio-only + low resolution (MAX_VIDEO_MB=512).
- Composio action names must come from `scripts/discover_composio.py` —
  recorded in findings before Milestone 5.
