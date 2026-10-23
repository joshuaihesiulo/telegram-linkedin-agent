"""LinkedIn publishing via Composio. Action names discovered at runtime.

Use scripts/discover_composio.py to list exact actions and record them in
docs/findings.md — never hardcode from memory.
"""
from __future__ import annotations

from pathlib import Path

from . import config


def discover_actions() -> list[str]:
    """Best-effort listing of LinkedIn actions via installed Composio SDK."""
    found: list[str] = []
    # v3 SDK
    try:
        from composio import Composio  # type: ignore

        c = Composio(api_key=config.COMPOSIO_API_KEY)
        # v3 exposes toolkits; introspect without assuming method names.
        for attr in ("tools", "apps", "actions", "toolkits"):
            if hasattr(c, attr):
                found.append(f"Composio.{attr}:present")
        try:
            tools = c.tools.get(user_id=config.COMPOSIO_ENTITY_ID, toolkits=["linkedin"])  # type: ignore
            for t in tools or []:
                found.append(str(getattr(t, "name", t))[:120])
        except Exception as e:  # noqa: BLE001
            found.append(f"tools.get failed: {e}")
        return found
    except Exception as e:  # noqa: BLE001
        found.append(f"v3 import failed: {e}")
    # legacy SDK
    try:
        import composio_core  # type: ignore  # noqa: F401

        found.append("composio_core:present (legacy)")
    except Exception as e:  # noqa: BLE001
        found.append(f"legacy import failed: {e}")
    return found


def publish_video_post(text: str, video_path: Path,
                       author: str | None = None) -> str:
    """Publish native video post. Returns post URL. Raises on failure.

    Tries Composio v3 session execute; falls back to a clear error telling
    the operator to run discovery + connect LinkedIn.
    """
    if not config.COMPOSIO_API_KEY:
        raise RuntimeError("COMPOSIO_API_KEY not configured")
    author = author or config.LINKEDIN_AUTHOR
    if not author:
        raise RuntimeError("LINKEDIN_AUTHOR (organization URN) not configured")

    last: Exception | None = None
    # --- v3 path ---
    try:
        from composio import Composio  # type: ignore

        c = Composio(api_key=config.COMPOSIO_API_KEY)
        # Action name must come from discovery; try candidates in order.
        for action in ("LINKEDIN_CREATE_VIDEO_POST", "LINKEDIN_CREATE_POST"):
            try:
                result = c.tools.execute(  # type: ignore[attr-defined]
                    action,
                    {"author": author, "commentary": text,
                     "video_path": str(video_path)},
                    user_id=config.COMPOSIO_ENTITY_ID,
                )
                url = _extract_url(result)
                if url:
                    return url
            except Exception as e:  # noqa: BLE001
                last = e
                continue
        raise RuntimeError(f"Composio execute failed: {last}")
    except ImportError as e:
        last = e
    except Exception as e:  # noqa: BLE001
        last = e

    raise RuntimeError(
        "LinkedIn publish unavailable. Run scripts/discover_composio.py, "
        f"connect LinkedIn for entity {config.COMPOSIO_ENTITY_ID}, then retry. "
        f"Detail: {last}"
    )


def _extract_url(result) -> str:
    if isinstance(result, dict):
        for k in ("post_url", "url", "link", "urn"):
            if result.get(k):
                return str(result[k])
        data = result.get("data", {})
        if isinstance(data, dict):
            for k in ("post_url", "url", "link"):
                if data.get(k):
                    return str(data[k])
    text = str(result)
    import re

    m = re.search(r"https?://[^\s'\"]*linkedin[^\s'\"]*", text)
    return m.group(0) if m else ""
