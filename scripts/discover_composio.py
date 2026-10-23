"""List available Composio LinkedIn actions. Record exact names in docs/findings.md.

Usage: venv\\Scripts\\python.exe scripts/discover_composio.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from app.linkedin import discover_actions

for line in discover_actions():
    print(line)
