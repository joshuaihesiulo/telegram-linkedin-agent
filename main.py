"""Telegram AI agent: Gemini + Composio.

Uses a Composio session scoped to the `telegram` toolkit and gives the
session tools to Gemini via automatic function calling (AFC).

You need a Telegram bot token from @BotFather - connect it once in
Composio, then the agent can send messages, photos, polls, read updates
and chat history. Note: bots cannot DM users who haven't messaged the
bot first; use TELEGRAM_GET_UPDATES to find valid chat_id values.

Setup:
    venv\\Scripts\\python.exe -m pip install -r requirements.txt
    copy .env.example .env   # then fill in keys
    venv\\Scripts\\python.exe main.py
"""

import os

from dotenv import load_dotenv
from google import genai
from google.genai import types

from composio import Composio
from composio_gemini import GeminiProvider

load_dotenv()

COMPOSIO_API_KEY = os.environ.get("COMPOSIO_API_KEY")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
USER_ID = os.environ.get("TELEGRAM_USER_ID", "telegram-user-1")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

SYSTEM_PROMPT = (
    "You are a Telegram assistant. Use the Composio Telegram tools to "
    "complete the request (send messages, photos, polls, check updates "
    "and chat history, manage chats). "
    "If a connection is required, share its Connect Link and wait. "
    "Ask for confirmation before sending any message."
)


def main() -> None:
    if not COMPOSIO_API_KEY:
        raise SystemExit("Missing COMPOSIO_API_KEY - copy .env.example to .env first.")
    if not GOOGLE_API_KEY:
        raise SystemExit("Missing GOOGLE_API_KEY - copy .env.example to .env first.")

    # Composio session scoped to Telegram for one end user.
    composio = Composio(provider=GeminiProvider(), api_key=COMPOSIO_API_KEY)
    session = composio.sessions.create(user_id=USER_ID, toolkits=["telegram"])
    tools = session.tools()  # AFC-ready callables for google-genai

    client = genai.Client(api_key=GOOGLE_API_KEY)
    chat = client.chats.create(
        model=MODEL,
        config=types.GenerateContentConfig(
            tools=tools,
            system_instruction=SYSTEM_PROMPT,
        ),
    )

    print("Telegram agent ready. Type 'exit' to quit.")
    print("Examples: 'send hello to chat 123456789', 'show recent updates'\n")
    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if user_input.lower() == "exit":
            break
        if not user_input:
            continue
        response = chat.send_message(user_input)
        print(f"Assistant: {response.text}\n")


if __name__ == "__main__":
    main()
