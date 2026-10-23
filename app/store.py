"""State + draft storage. Supabase REST when configured, else in-memory fallback.

Ephemeral disk reminder: never rely on memory/disk in production - set
SUPABASE_URL + SUPABASE_KEY so state survives restarts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from . import config
from .schemas import DraftRecord, UserState, VideoAnalysis

_CORE_FIELDS = {"telegram_id", "state", "instructions", "drive_file_id"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _profile(s: UserState) -> dict:
    """Everything except the columns that already exist in user_state."""
    return {k: v for k, v in s.model_dump(mode="json").items()
            if k not in _CORE_FIELDS}


def _state_from_row(row: dict) -> UserState:
    data = {
        "telegram_id": row["telegram_id"],
        "state": row.get("state", "IDLE"),
        "instructions": row.get("instructions", ""),
        "drive_file_id": row.get("drive_file_id", ""),
    }
    profile = row.get("profile") or {}
    if isinstance(profile, dict):
        data.update(profile)
    return UserState.model_validate(data)


def _draft_from_row(row: dict) -> DraftRecord:
    return DraftRecord(
        draft_id=row["draft_id"],
        telegram_id=row["telegram_id"],
        draft_text=row["draft_text"],
        status=row.get("status", "pending"),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        drive_file_id=row.get("drive_file_id") or "",
        analysis_json=(VideoAnalysis.model_validate(row["analysis_json"])
                       if row.get("analysis_json") else None),
        platform=row.get("platform") or "",
        media_type=row.get("media_type") or "text",
        media_file_id=row.get("media_file_id") or "",
        media_url=row.get("media_url") or "",
        scheduled_for=row.get("scheduled_for"),
        post_url=row.get("post_url") or "",
    )


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
        if d and d.expires_at < _utcnow() and d.status in ("pending", "scheduled"):
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

    def due_scheduled(self, now: datetime | None = None) -> list[DraftRecord]:
        now = now or _utcnow()
        due = [
            d for d in self.drafts.values()
            if d.status == "scheduled" and d.scheduled_for
            and d.scheduled_for <= now
        ]
        due.sort(key=lambda d: d.scheduled_for or now)
        return due


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
                return _state_from_row(r.json()[0])
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
                    "profile": _profile(s),
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
                    "analysis_json": (d.analysis_json.model_dump()
                                      if d.analysis_json else None),
                    "draft_text": d.draft_text,
                    "status": d.status,
                    "created_at": d.created_at.isoformat(),
                    "expires_at": d.expires_at.isoformat(),
                    "post_url": d.post_url,
                    "platform": d.platform,
                    "media_type": d.media_type,
                    "media_file_id": d.media_file_id,
                    "media_url": d.media_url,
                    "scheduled_for": (d.scheduled_for.isoformat()
                                      if d.scheduled_for else None),
                },
                timeout=10,
            )
        except Exception:
            pass

    def get_draft(self, draft_id: str) -> DraftRecord | None:
        d = super().get_draft(draft_id)
        if d is not None:
            return d
        if not self.enabled:
            return None
        try:
            r = httpx.get(
                f"{self.base}/rest/v1/drafts?draft_id=eq.{draft_id}",
                headers=self._headers(), timeout=10,
            )
            if r.status_code == 200 and r.json():
                d = _draft_from_row(r.json()[0])
                super().save_draft(d)
                return super().get_draft(draft_id)
        except Exception:
            pass
        return None

    def due_scheduled(self, now: datetime | None = None) -> list[DraftRecord]:
        now = now or _utcnow()
        rows: list[DraftRecord] = []
        if self.enabled:
            try:
                r = httpx.get(
                    f"{self.base}/rest/v1/drafts",
                    params={
                        "select": "*",
                        "status": "eq.scheduled",
                        "scheduled_for": f"lte.{now.isoformat()}",
                        "order": "scheduled_for.asc",
                    },
                    headers=self._headers(), timeout=10,
                )
                if r.status_code == 200:
                    rows = [_draft_from_row(row) for row in r.json()]
                    for d in rows:
                        super().save_draft(d)
            except Exception:
                rows = []
        return rows or super().due_scheduled(now)


def new_draft_id() -> str:
    import uuid

    return "d_" + uuid.uuid4().hex[:12]


def expiry(ttl_hours: int | None = None) -> datetime:
    hours = ttl_hours if ttl_hours is not None else config.DRAFT_TTL_HOURS
    return _utcnow() + timedelta(hours=hours)


store = SupabaseStore()
