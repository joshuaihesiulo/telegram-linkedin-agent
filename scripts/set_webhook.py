"""Set the Telegram webhook with secret token. Run after each deploy.

Usage: venv\\Scripts\\python.exe scripts/set_webhook.py
Needs TELEGRAM_BOT_TOKEN, PUBLIC_BASE_URL, TELEGRAM_WEBHOOK_SECRET in .env.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from dotenv import load_dotenv

load_dotenv()

token = os.environ["TELEGRAM_BOT_TOKEN"]
base = os.environ["PUBLIC_BASE_URL"].rstrip("/")
secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

r = httpx.post(
    f"https://api.telegram.org/bot{token}/setWebhook",
    json={"url": f"{base}/webhook", "secret_token": secret},
    timeout=20,
)
print(r.status_code, r.text)
