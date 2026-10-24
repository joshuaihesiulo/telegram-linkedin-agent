"""Pydantic gates between every phase. Fail loudly on invalid data."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

PostingMode = Literal["on_demand", "scheduled"]
MediaPreference = Literal["text", "image", "video", "image_video"]


class VideoAnalysis(BaseModel):
    transcript: str = Field(min_length=1)
    key_points: list[str] = Field(min_length=3, max_length=5)
    main_takeaway: str = Field(min_length=1)
    on_screen_text: list[str] = Field(default_factory=list)
    tone: str = Field(min_length=1)
    names_numbers_claims: list[str] = Field(default_factory=list)
    topic_tags: list[str] = Field(default_factory=list)
    uncertain_items: list[str] = Field(default_factory=list)


class DraftPost(BaseModel):
    hook: str = Field(min_length=1)
    body: str = Field(min_length=1)
    cta: str = Field(min_length=1)
    hashtags: list[str] = Field(min_length=3, max_length=5)
    verify_block: list[str] = Field(default_factory=list)

    @field_validator("hashtags")
    @classmethod
    def _hash_format(cls, v: list[str]) -> list[str]:
        out = []
        for h in v:
            h = h.strip()
            if not h.startswith("#"):
                h = "#" + h
            out.append(h)
        return out

    def full_text(self) -> str:
        tags = " ".join(self.hashtags)
        return f"{self.hook}\n\n{self.body}\n\n{self.cta}\n\n{tags}"

    def char_count(self) -> int:
        return len(self.full_text())


class UserState(BaseModel):
    telegram_id: int
    state: Literal[
        "IDLE", "AWAITING_LINK", "PROCESSING",
        "AWAITING_APPROVAL", "PUBLISHED", "EDITING",
        "REJECTED", "EXPIRED", "FAILED",
        "ONBOARDING", "AWAITING_MATERIAL", "SCHEDULED",
    ] = "IDLE"
    instructions: str = ""
    drive_file_id: str = ""

    # --- onboarding profile ---------------------------------------------
    onboarded: bool = False
    onboarding_step: str = ""
    invite_sent: bool = False   # first-contact welcome with the web link sent
    platform: str = ""
    account: str = ""
    account_label: str = ""
    connect_url: str = ""
    samples: list[str] = Field(default_factory=list)
    voice_guide: str = ""
    posting_mode: PostingMode = "on_demand"
    schedule: str = ""
    timezone: str = "UTC"
    media_pref: MediaPreference = "text"
    # Material waiting for a caption / media before drafting.
    pending_text: str = ""
    pending_media_file_id: str = ""
    pending_media_type: str = ""
    pending_media_url: str = ""


class DraftRecord(BaseModel):
    draft_id: str
    telegram_id: int
    draft_text: str
    status: Literal[
        "pending", "approved", "scheduled", "publishing", "rejected",
        "expired", "published", "failed",
    ] = "pending"
    created_at: datetime
    expires_at: datetime
    drive_file_id: str = ""
    analysis_json: VideoAnalysis | None = None
    platform: str = ""
    media_type: str = "text"          # text | image | video
    media_file_id: str = ""
    media_url: str = ""
    scheduled_for: datetime | None = None
    post_url: str = ""
