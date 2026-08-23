"""Pydantic models for the DeviantArt API surface we consume.

Every model ignores unknown fields. The API gains fields over time, and a
scheduled job must not start failing because DeviantArt shipped a feature.
"""

from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_TOLERANT = ConfigDict(extra="ignore")


class Author(BaseModel):
    model_config = _TOLERANT

    userid: str | None = None
    username: str = "unknown"


class MediaRef(BaseModel):
    """A preview or full-content image reference."""

    model_config = _TOLERANT

    src: str | None = None


class Deviation(BaseModel):
    model_config = _TOLERANT

    deviationid: str
    title: str = "Untitled"
    url: str | None = None
    author: Author = Field(default_factory=Author)
    is_mature: bool = False
    published_time: str | None = None
    preview: MediaRef | None = None
    content: MediaRef | None = None

    @property
    def author_name(self) -> str:
        return self.author.username

    @property
    def image_url(self) -> str | None:
        """Best available image for a notification attachment.

        The preview is preferred: it is smaller, and a notification thumbnail
        does not benefit from a multi-megabyte original.
        """
        if self.preview is not None and self.preview.src:
            return self.preview.src
        if self.content is not None and self.content.src:
            return self.content.src
        return None


class DailyDeviationsPage(BaseModel):
    model_config = _TOLERANT

    results: list[Deviation] = Field(default_factory=list)
    has_more: bool = False


class Token(BaseModel):
    """An OAuth2 access token and the moment it stops being usable."""

    access_token: str
    expires_at: datetime

    @classmethod
    def from_response(cls, payload: dict[str, Any], now: datetime) -> "Token":
        expires_in = int(payload.get("expires_in", 3600))
        return cls(
            access_token=str(payload["access_token"]),
            expires_at=now + timedelta(seconds=expires_in),
        )

    def is_valid(self, now: datetime, leeway_seconds: int = 60) -> bool:
        """True if the token has more than ``leeway_seconds`` of life left.

        The leeway prevents a token that expires mid-run from causing a 401
        halfway through a batch of notifications.
        """
        return self.expires_at - timedelta(seconds=leeway_seconds) > now
