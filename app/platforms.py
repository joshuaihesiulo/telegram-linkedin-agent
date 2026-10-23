"""Platform registry plus connect/publish adapters.

Composio is the only integration layer: one Connect Link per platform, one
session tool call per action. Action names come from the live toolkit docs
(recorded in docs/findings.md), never from memory.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path

from . import composio_client, config

log = logging.getLogger("platforms")


@dataclass(frozen=True)
class Platform:
    slug: str
    name: str
    toolkit: str
    max_chars: int
    media: tuple[str, ...]
    needs_account: bool
    account_hint: str = ""
    note: str = ""


PLATFORMS: tuple[Platform, ...] = (
    Platform("linkedin", "LinkedIn", "LINKEDIN", 3000,
             ("text", "image", "video"), True,
             "person or company URN"),
    Platform("twitter", "X (Twitter)", "TWITTER", 280,
             ("text", "image", "video"), False,
             note="X requires your own API credentials in Composio."),
    Platform("facebook", "Facebook Page", "FACEBOOK", 5000,
             ("text", "image", "video"), True, "Page ID"),
    Platform("instagram", "Instagram", "INSTAGRAM", 2200,
             ("image", "video"), True, "Instagram Business account ID",
             note="Instagram needs a public image/video URL."),
)

_BY_SLUG = {p.slug: p for p in PLATFORMS}


@dataclass(frozen=True)
class Account:
    value: str
    label: str


def get(slug: str) -> Platform | None:
    return _BY_SLUG.get(slug)


def choices() -> list[Platform]:
    return list(PLATFORMS)


def connect_url(user_id: int, slug: str) -> str:
    p = _BY_SLUG[slug]
    return composio_client.start_connection(user_id, p.toolkit)


def is_connected(user_id: int, slug: str) -> bool:
    p = _BY_SLUG[slug]
    return composio_client.is_connected(user_id, p.toolkit)


def _payload(obj):
    """Unwrap a Composio result object to its ``data`` payload."""
    return getattr(obj, "data", obj)


def _walk(obj):
    obj = _payload(obj)
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v)
    elif isinstance(obj, str) and len(obj) < 200_000:
        # Tool data is often a JSON string; look inside it too.
        s = obj.strip()
        if s.startswith(("{", "[")):
            try:
                parsed = json.loads(s)
            except ValueError:
                return
            if isinstance(parsed, (dict, list)):
                yield from _walk(parsed)


def _strings(obj) -> list[str]:
    obj = _payload(obj)
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith(("{", "[")):
            try:
                return _strings(json.loads(s))
            except ValueError:
                return [obj]
        return [obj]
    if isinstance(obj, dict):
        out: list[str] = []
        for v in obj.values():
            out.extend(_strings(v))
        return out
    if isinstance(obj, (list, tuple)):
        out = []
        for v in obj:
            out.extend(_strings(v))
        return out
    return []


def _dicts_with(obj, keys: tuple[str, ...]) -> list[dict]:
    return [d for d in _walk(obj) if all(k in d for k in keys)]


def _first_value(obj, names: tuple[str, ...]) -> str:
    for d in _walk(obj):
        for name in names:
            v = d.get(name)
            if isinstance(v, (str, int)) and str(v).strip():
                return str(v).strip()
    return ""


def _execute(user_id: int, toolkit: str, action: str, args: dict | None = None):
    return composio_client.execute(user_id, toolkit, action, args or {})


def detect_accounts(user_id: int, slug: str) -> list[Account]:
    """Best-effort account discovery after OAuth. Never blocks onboarding."""
    p = _BY_SLUG[slug]
    found: list[Account] = []
    try:
        if slug == "linkedin":
            data = _execute(user_id, p.toolkit, "LINKEDIN_GET_MY_INFO")
            pid = _first_value(data, ("id", "person_id", "sub"))
            name = _first_value(data, ("name", "displayName", "localizedFirstName"))
            if pid:
                urn = pid if pid.startswith("urn:li:person:") else f"urn:li:person:{pid}"
                found.append(Account(urn, name or "Your LinkedIn profile"))
            orgs = _execute(user_id, p.toolkit, "LINKEDIN_GET_COMPANY_INFO")
            for s in _strings(orgs):
                if s.startswith("urn:li:organization:"):
                    found.append(Account(s, f"Company page {s.rsplit(':', 1)[-1]}"))
        elif slug == "facebook":
            data = _execute(user_id, p.toolkit, "FACEBOOK_LIST_MANAGED_PAGES")
            for d in _dicts_with(data, ("id", "name")):
                pid = str(d["id"])
                if pid.isdigit() and len(pid) >= 5:
                    found.append(Account(pid, str(d["name"])))
        elif slug == "instagram":
            data = _execute(user_id, p.toolkit, "INSTAGRAM_GET_USER_INFO",
                            {"ig_user_id": "me"})
            iid = _first_value(data, ("id", "user_id"))
            uname = _first_value(data, ("username", "name"))
            if iid:
                found.append(Account(iid, f"@{uname}" if uname else "Instagram account"))
    except Exception as e:  # noqa: BLE001
        log.warning("account discovery failed for %s: %s", slug, e)

    if slug == "linkedin" and config.LINKEDIN_AUTHOR:
        if not any(a.value == config.LINKEDIN_AUTHOR for a in found):
            found.insert(0, Account(config.LINKEDIN_AUTHOR, "Configured company page"))

    seen: set[str] = set()
    unique = []
    for a in found:
        if a.value not in seen:
            seen.add(a.value)
            unique.append(a)
    return unique


def _media_obj(toolkit: str, action: str, path: Path) -> dict:
    """Stage a local media file with Composio for one tool action.

    Returns the ``{name, mimetype, s3key}`` descriptor those schemas expect.
    """
    return composio_client.upload_file(toolkit, action, path)


def _result_url(result) -> str:
    data = getattr(result, "data", result)
    for d in _walk(data):
        for key in ("post_url", "permalink", "url", "link", "web_url"):
            v = d.get(key)
            if isinstance(v, str) and v.startswith("http"):
                return v
    return ""


def _ok(result) -> bool:
    return bool(getattr(result, "successful", True))


def _error(result) -> str:
    return str(getattr(result, "error", "") or "")


def publish(user_id: int, platform: str, account: str, text: str,
            media_path: Path | None = None, media_type: str = "text",
            media_url: str = "") -> str:
    """Publish text or media. Returns a URL when the provider gives one."""
    p = _BY_SLUG.get(platform)
    if not p:
        raise RuntimeError(f"Unsupported platform: {platform}")
    if platform == "linkedin":
        return _publish_linkedin(user_id, account, text, media_path,
                                 media_type, media_url)
    if platform == "twitter":
        return _publish_twitter(user_id, text, media_path, media_type)
    if platform == "facebook":
        return _publish_facebook(user_id, account, text, media_path, media_type)
    if platform == "instagram":
        return _publish_instagram(user_id, account, text, media_url, media_type)
    raise RuntimeError(f"Unsupported platform: {platform}")


def _publish_linkedin(user_id: int, author: str, text: str,
                      media_path: Path | None, media_type: str,
                      media_url: str) -> str:
    if media_type == "video":
        if not media_path and not media_url:
            raise RuntimeError("LinkedIn video needs a file or public URL")
        from .linkedin import publish_session_video

        return publish_session_video(user_id, text, video_path=media_path,
                                     video_url=media_url, author=author)
    args = {"author": author, "commentary": text, "visibility": "PUBLIC"}
    if media_type == "image" and media_path:
        args["images"] = [_linkedin_image_urn(user_id, author, media_path)]
    result = _execute(user_id, "LINKEDIN",
                      "LINKEDIN_CREATE_LINKED_IN_POST", args)
    if not _ok(result):
        raise RuntimeError(_error(result) or "LinkedIn rejected the post")
    return _result_url(result)


def _linkedin_image_urn(user_id: int, owner: str, path: Path) -> str:
    data = _execute(user_id, "LINKEDIN", "LINKEDIN_INITIALIZE_IMAGE_UPLOAD",
                    {"owner": owner})
    upload_url = _first_value(data, ("upload_url", "uploadUrl", "uploadURL"))
    urn = _first_value(data, ("image_urn", "imageUrn", "asset_urn", "urn"))
    if not upload_url:
        data = _execute(user_id, "LINKEDIN", "LINKEDIN_REGISTER_IMAGE_UPLOAD",
                        {"owner_urn": owner})
        upload_url = _first_value(data, ("upload_url", "uploadUrl", "uploadURL"))
        urn = _first_value(data, ("asset_urn", "image_urn", "urn"))
    if not upload_url or not urn:
        raise RuntimeError("LinkedIn image upload did not return a URL and URN")
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    import httpx

    r = httpx.put(upload_url, content=path.read_bytes(),
                  headers={"Content-Type": mime}, timeout=120)
    r.raise_for_status()
    return urn


def _await_x_media(user_id: int, media_id: str, timeout: float = 150.0) -> None:
    """Poll X media processing until ready (videos/GIFs/large images).

    Best-effort: unknown status shapes return early so a parsing change can
    never stall publishing; only explicit pending states keep waiting.
    """
    interval = 5.0
    deadline = time.monotonic() + timeout
    while True:
        st = _execute(user_id, "TWITTER", "TWITTER_GET_MEDIA_UPLOAD_STATUS",
                      {"media_id": media_id})
        state = _first_value(getattr(st, "data", st),
                             ("state", "processing_state")).lower()
        if state in ("succeeded", "success", "ready"):
            return
        if state in ("failed", "error"):
            raise RuntimeError("X could not process the media. Try again.")
        if state not in ("pending", "in_progress", "processing"):
            return
        if time.monotonic() >= deadline:
            log.warning("X media %s still processing after %.0fs",
                        media_id, timeout)
            return
        time.sleep(interval)


def _publish_twitter(user_id: int, text: str, media_path: Path | None,
                     media_type: str) -> str:
    args: dict = {"text": text}
    if media_path and media_type in ("image", "video"):
        # Videos/GIFs and images over ~5 MB must use the chunked endpoint.
        large = (media_type == "video"
                 or media_path.stat().st_size > 5 * 1024 * 1024)
        action = "TWITTER_UPLOAD_LARGE_MEDIA" if large else "TWITTER_UPLOAD_MEDIA"
        up = _execute(user_id, "TWITTER", action, {
            "media": _media_obj("TWITTER", action, media_path),
            "media_category": ("tweet_video" if media_type == "video"
                               else "tweet_image"),
        })
        if not _ok(up):
            raise RuntimeError(_error(up) or "X media upload failed")
        mid = _first_value(getattr(up, "data", up),
                           ("media_id_string", "media_id", "id"))
        if not mid:
            raise RuntimeError("X media upload returned no media ID")
        if large:
            _await_x_media(user_id, mid)
        args["media_media_ids"] = [mid]
    result = _execute(user_id, "TWITTER",
                      "TWITTER_CREATION_OF_A_POST", args)
    if not _ok(result):
        raise RuntimeError(_error(result) or "X rejected the post")
    return _result_url(result)


def _publish_facebook(user_id: int, page_id: str, text: str,
                      media_path: Path | None, media_type: str) -> str:
    if media_type == "image" and media_path:
        result = _execute(user_id, "FACEBOOK", "FACEBOOK_CREATE_PHOTO_POST", {
            "page_id": page_id, "message": text,
            "photo": _media_obj("FACEBOOK", "FACEBOOK_CREATE_PHOTO_POST",
                                media_path),
        })
    elif media_type == "video" and media_path:
        result = _execute(user_id, "FACEBOOK", "FACEBOOK_CREATE_VIDEO_POST", {
            "page_id": page_id, "description": text,
            "video": _media_obj("FACEBOOK", "FACEBOOK_CREATE_VIDEO_POST",
                                media_path),
        })
    else:
        result = _execute(user_id, "FACEBOOK", "FACEBOOK_CREATE_POST", {
            "page_id": page_id, "message": text,
        })
    if not _ok(result):
        raise RuntimeError(_error(result) or "Facebook rejected the post")
    return _result_url(result)


def _publish_instagram(user_id: int, ig_user_id: str, caption: str,
                       media_url: str, media_type: str) -> str:
    if not media_url:
        raise RuntimeError(
            "Instagram needs a public image/video URL. Send one with the post.")
    kind = "video_url" if media_type == "video" else "image_url"
    container = _execute(user_id, "INSTAGRAM", "INSTAGRAM_POST_IG_USER_MEDIA", {
        "ig_user_id": ig_user_id, "caption": caption, kind: media_url,
    })
    if not _ok(container):
        raise RuntimeError(_error(container) or "Instagram container failed")
    creation_id = _first_value(container.data if hasattr(container, "data")
                               else container, ("id", "creation_id"))
    if not creation_id:
        raise RuntimeError("Instagram returned no creation ID")
    # The publish action polls the container until FINISHED, so video works.
    result = _execute(user_id, "INSTAGRAM",
                      "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH", {
                          "ig_user_id": ig_user_id,
                          "creation_id": creation_id,
                          "max_wait_seconds": 300 if media_type == "video" else 60,
                      })
    if not _ok(result):
        raise RuntimeError(_error(result) or "Instagram rejected the post")
    return _result_url(result)
