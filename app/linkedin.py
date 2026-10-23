"""LinkedIn publishing via Composio.

Action names are confirmed against the live toolkit docs (docs/findings.md):
- LINKEDIN_UPLOAD_VIDEO   file (staged s3key descriptor) or public video_url
                          -> waits until the video is ready
- LINKEDIN_CREATE_VIDEO_POST  video_urn + commentary  (member-owned only)
- LINKEDIN_INITIALIZE_IMAGE_UPLOAD / LINKEDIN_REGISTER_IMAGE_UPLOAD
                          -> presigned upload URL + image URN
- LINKEDIN_CREATE_LINKED_IN_POST  author + commentary [+ images: URNs]
"""
from __future__ import annotations

import re
from pathlib import Path

from . import composio_client, config


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


def _ok(result) -> bool:
    return bool(getattr(result, "successful", True))


def _error(result) -> str:
    return str(getattr(result, "error", "") or "")


def _data(result):
    return getattr(result, "data", result)


def extract_video_urn(result) -> str:
    """Find the LinkedIn video URN in an upload response.

    The action's ``data`` is often a JSON string, so search structured
    values first and fall back to a regex over the raw payload.
    """
    data = _data(result)
    stack = [data]
    while stack:
        obj = stack.pop()
        if isinstance(obj, dict):
            for key in ("video_urn", "videoUrn", "urn", "id"):
                v = obj.get(key)
                if isinstance(v, str) and v.startswith("urn:li:video:"):
                    return v
            stack.extend(obj.values())
        elif isinstance(obj, (list, tuple)):
            stack.extend(obj)
    m = re.search(r"urn:li:video:[\w:-]+", str(data))
    return m.group(0) if m else ""


def _extract_url(result) -> str:
    """Post URL if the response carries one, else ''."""
    data = _data(result)
    if isinstance(data, dict):
        for key in ("post_url", "url", "link", "permalink"):
            v = data.get(key)
            if isinstance(v, str) and v.startswith("http"):
                return v
    text = str(data)
    m = re.search(r"https?://[^\s'\"]*linkedin[^\s'\"]*", text)
    return m.group(0) if m else ""


def publish_session_video(user_id: int, text: str,
                          video_path: Path | None = None,
                          video_url: str = "", author: str = "") -> str:
    """Publish a native video post for one Telegram user's session.

    LinkedIn's toolkit uploads *member-owned* videos. Company-page native
    video is not exposed, so an org author gets a clear, actionable error
    instead of an opaque API failure.
    """
    if author.startswith("urn:li:organization:"):
        raise RuntimeError(
            "LinkedIn's toolkit can only publish native video to a personal "
            "profile, not a company page. Pick your profile in /settings, or "
            "attach an image for a company-page post.")
    args: dict = {}
    if video_path is not None:
        args["file"] = composio_client.upload_file(
            "LINKEDIN", "LINKEDIN_UPLOAD_VIDEO", video_path)
    elif video_url:
        args["video_url"] = video_url
    else:
        raise RuntimeError("LinkedIn video needs a file or public URL")

    up = composio_client.execute(user_id, "LINKEDIN",
                                 "LINKEDIN_UPLOAD_VIDEO", args)
    if not _ok(up):
        raise RuntimeError(_error(up) or "LinkedIn video upload failed")
    video_urn = extract_video_urn(up)
    if not video_urn:
        raise RuntimeError("LinkedIn video upload returned no video URN")

    result = composio_client.execute(user_id, "LINKEDIN",
                                     "LINKEDIN_CREATE_VIDEO_POST", {
                                         "video_urn": video_urn,
                                         "commentary": text,
                                         "visibility": "PUBLIC",
                                     })
    if not _ok(result):
        raise RuntimeError(_error(result) or "LinkedIn rejected the video post")
    return _extract_url(result)


def publish_video_post(text: str, video_path: Path,
                       author: str | None = None) -> str:
    """Legacy entrypoint for pre-onboarding drafts on the shared entity.

    New posts go through ``publish_session_video`` with the user's own
    connection; this keeps the old approval path working.
    """
    if not config.COMPOSIO_API_KEY:
        raise RuntimeError("COMPOSIO_API_KEY not configured")
    try:
        from composio import Composio  # type: ignore

        client = Composio(api_key=config.COMPOSIO_API_KEY)
        file_obj = composio_client.upload_file(
            "LINKEDIN", "LINKEDIN_UPLOAD_VIDEO", video_path)
        up = client.tools.execute(
            "LINKEDIN_UPLOAD_VIDEO", {"file": file_obj},
            user_id=config.COMPOSIO_ENTITY_ID)
        if not _ok(up):
            raise RuntimeError(_error(up) or "LinkedIn video upload failed")
        video_urn = extract_video_urn(up)
        if not video_urn:
            raise RuntimeError("LinkedIn video upload returned no video URN")
        result = client.tools.execute(
            "LINKEDIN_CREATE_VIDEO_POST",
            {"video_urn": video_urn, "commentary": text, "visibility": "PUBLIC"},
            user_id=config.COMPOSIO_ENTITY_ID)
        if not _ok(result):
            raise RuntimeError(_error(result) or "LinkedIn rejected the post")
        return _extract_url(result)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "LinkedIn video publish failed for the legacy shared account. "
            f"Connect LinkedIn for entity {config.COMPOSIO_ENTITY_ID} and "
            f"retry. Detail: {e}") from e
