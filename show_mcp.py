"""Print a Composio session MCP endpoint for the Telegram toolkit.

Point any MCP-compatible client (Cursor, Claude Desktop, VS Code, ...)
at the printed URL + headers instead of wiring the SDK into code:

    venv\\Scripts\\python.exe show_mcp.py
"""

import os

from dotenv import load_dotenv

from composio import Composio

load_dotenv()

composio = Composio()  # uses COMPOSIO_API_KEY from .env
user_id = os.environ.get("TELEGRAM_USER_ID", "telegram-user-1")

session = composio.sessions.create(
    user_id=user_id,
    toolkits=["telegram"],
    mcp=True,
)

print("MCP URL:", session.mcp.url)
print("MCP headers:", session.mcp.headers)
print("Session ID:", session.session_id)
