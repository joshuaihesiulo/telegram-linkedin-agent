"""State + draft storage. Supabase REST when configured, else in-memory fallback.

Ephemeral disk reminder: never rely on memory/disk in production — set
SUPABASE_URL + SUPABASE_KEY so state survives restarts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from . import config
from .schemas import DraftRecord, UserState, VideoAnalysis


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MemoryStore:
    def __init__(self) -> None:
        self.states: dict[int, UserState] = {}
        self.drafts: dict[str, DraftRecord] = {}
        self.updates: set[int] = set()

    # -- state --
    def get_state(self, telegram_id: int) -> UserState:
        return self.states.get(telegram_id, UserState(telegram_id=telegram_id))

    def set_state(self, s: UserState) -> None:
        self.states[s.telegram_id] = s

    # -- dedupe --
    def seen_update(self, update_id: int) -> bool:
        if update_id in self.updates:
            return True
        self.updates.add(update_id)
        return False

    # -- drafts --
    def save_draft(self, d: DraftRecord) -> None:
        self.drafts[d.draft_id] = d

    def get_draft(self, draft_id: str) -> DraftRecord | None:
        d = self.drafts.get(draft_id)
        if d and d.expires_at < _utcnow():
            d.status = "expired"
        return d

    def latest_pending(self, telegram_id: int) -> DraftRecord | None:
        cands = [
            d for d in self.drafts.values()
            if d.telegram_id == telegram_id and d.status == "pending"
        ]
        if not cands:
            return None
        cands.sort(key=lambda d: d.created_at, reverse=True)
        d = cands[0]
        if d.expires_at < _utcnow():
            d.status = "expired"
            return None
        return d


class SupabaseStore(MemoryStore):
    """Thin REST wrapper. Falls back to memory rows if a request fails,
    so a Supabase outage degrades instead of losing the webhook."""

    def __init__(self) -> None:
        super().__init__()
        self.base = config.SUPABASE_URL
        self.key = config.SUPABASE_KEY

    @property
    def enabled(self) -> bool:
        return bool(self.base and self.key)

    def _headers(self) -> dict:
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }

    # state
    def get_state(self, telegram_id: int) -> UserState:
        if not self.enabled:
            return super().get_state(telegram_id)
        try:
            r = httpx.get(
                f"{self.base}/rest/v1/user_state?telegram_id=eq.{telegram_id}",
                headers=self._headers(), timeout=10,
            )
            if r.status_code == 200 and r.json():
                row = r.json()[0]
                return UserState(
                    telegram_id=telegram_id,
                    state=row.get("state", "IDLE"),
                    instructions=row.get("instructions", ""),
                    drive_file_id=row.get("drive_file_id", ""),
                )
        except Exception:
            pass
        return super().get_state(telegram_id)

    def set_state(self, s: UserState) -> None:
        super().set_state(s)
        if not self.enabled:
            return
        try:
            httpx.post(
                f"{self.base}/rest/v1/user_state",
                headers={**self._headers(), "Prefer": "resolution=merge-duplicates"},
                json={
                    "telegram_id": s.telegram_id,
                    "state": s.state,
                    "instructions": s.instructions,
                    "drive_file_id": s.drive_file_id,
                    "updated_at": _utcnow().isoformat(),
                },
                timeout=10,
            )
        except Exception:
            pass

    def seen_update(self, update_id: int) -> bool:
        if update_id in self.updates:
            return True
        self.updates.add(update_id)
        if not self.enabled:
            return False
        try:
            r = httpx.post(
                f"{self.base}/rest/v1/processed_updates",
                headers={**self._headers(), "Prefer": "resolution=ignore-duplicates"},
                json={"update_id": update_id},
                timeout=10,
            )
            # 409 = already existed
            return r.status_code == 409
        except Exception:
            return False

    def save_draft(self, d: DraftRecord) -> None:
        super().save_draft(d)
        if not self.enabled:
            return
        try:
            httpx.post(
                f"{self.base}/rest/v1/drafts",
                headers={**self._headers(), "Prefer": "resolution=merge-duplicates"},
                json={
                    "draft_id": d.draft_id,
                    "telegram_id": d.telegram_id,
                    "drive_file_id": d.drive_file_id,
                    "analysis_json": d.analysis_json.model_dump(),
                    "draft_text": d.draft_text,
                    "status": d.status,
                    "created_at": d.created_at.isoformat(),
                    "expires_at": d.expires_at.isoformat(),
                    "post_url": d.post_url,
                },
                timeout=10,
            )
        except Exception:
            pass


def new_draft_id() -> str:
    import uuid

    return "d_" + uuid.uuid4().hex[:12]


def expiry(ttl_hours: int | None = None) -> datetime:
    hours = ttl_hours if ttl_hours is not None else config.DRAFT_TTL_HOURS
    return _utcnow() + timedelta(hours=hours)


store = SupabaseStore()
