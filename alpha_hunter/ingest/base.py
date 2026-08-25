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


@dataclass
class SourceAttempt:
    """Bir kaynagin tek bir denemesinin sonucu.

    Sifir donusun SEBEBINI tasir: kaynak kapali miydi, hata mi verdi, yoksa
    calisip bos mu dondu? Uc durumun cozumu de farkli, o yuzden ayirt edilmeli.
    """

    source: str
    target: str
    count: int = 0
    skipped: bool = False        # kaynak kapali (anahtar yok vb.)
    error: str | None = None
    detail: str | None = None    # kaynagin kendi acikladigi sebep

    def summary(self) -> str:
        if self.skipped:
            return f"{self.source}: kapali"
        if self.error:
            return f"{self.source}: hata — {self.error}"
        if self.count:
            return f"{self.source}: {self.count} tweet"
        return f"{self.source}: 0 tweet" + (f" — {self.detail}" if self.detail else "")


@runtime_checkable
class TweetSource(Protocol):
    name: str
    # Kaynak son cagrida neyin ters gittigini buraya yazar
    last_detail: str | None

    async def search(self, query: str, since: datetime, limit: int) -> AsyncIterator[RawTweet]:
        ...

    async def user_timeline(self, handle: str, since: datetime, limit: int) -> AsyncIterator[RawTweet]:
        ...

    async def available(self) -> bool:
        ...
