"""Tweet kaynagi sozlesmesi. Butun kaziyicilar ayni `RawTweet`i uretir."""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass
class RawTweet:
    tweet_id: str
    handle: str                       # @ olmadan, kucuk harf
    posted_at: datetime               # UTC -- ALGORITMANIN TEMELI
    text: str
    url: str | None = None
    source: str = "unknown"
    lang: str | None = None
    platform_user_id: str | None = None
    display_name: str | None = None
    followers: int | None = None
    following: int | None = None
    account_created_at: datetime | None = None
    like_count: int | None = None
    retweet_count: int | None = None
    reply_count: int | None = None
    view_count: int | None = None
    is_retweet: bool = False
    is_reply: bool = False
    is_quote: bool = False
    expanded_urls: list[str] = field(default_factory=list)
    raw: dict | None = None


@runtime_checkable
class TweetSource(Protocol):
    name: str

    async def search(self, query: str, since: datetime, limit: int) -> AsyncIterator[RawTweet]:
        ...

    async def user_timeline(self, handle: str, since: datetime, limit: int) -> AsyncIterator[RawTweet]:
        ...

    async def available(self) -> bool:
        ...
