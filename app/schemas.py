"""Pydantic gates between every phase. Fail loudly on invalid data."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


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
    ] = "IDLE"
    instructions: str = ""
    drive_file_id: str = ""


class DraftRecord(BaseModel):
    draft_id: str
    telegram_id: int
    drive_file_id: str
    analysis_json: VideoAnalysis
    draft_text: str
    status: Literal[
        "pending", "approved", "rejected", "expired", "published", "failed"
    ] = "pending"
    created_at: datetime
    expires_at: datetime
    post_url: str = ""
