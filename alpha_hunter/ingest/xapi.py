"""Resmi X API v2 (recent search). Bearer token varsa kullanilir.

Limitler cok kati (free tier'da arama yok), o yuzden yedek kaynak olarak durur.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

from ..config import settings
from ..http import HttpClient
from .base import RawTweet

log = logging.getLogger(__name__)
_BASE = "https://api.x.com/2"


class XApiSource:
    name = "xapi"

    def __init__(self, http: HttpClient, bearer: str | None = None) -> None:
        self.http = http
        self.bearer = bearer or settings.x_bearer_token

    async def available(self) -> bool:
        return bool(self.bearer)

    async def _search(self, query: str, since: datetime, limit: int) -> list[RawTweet]:
        if not self.bearer:
            return []
        # recent search yalnizca son 7 gunu kapsar
        floor = datetime.now(timezone.utc) - timedelta(days=6, hours=23)
        start = max(since, floor)
        params = {
            "query": query,
            "max_results": str(min(100, max(10, limit))),
            "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tweet.fields": "created_at,public_metrics,entities,lang,referenced_tweets",
            "expansions": "author_id",
            "user.fields": "username,name,public_metrics,created_at",
        }
        out: list[RawTweet] = []
        next_token: str | None = None
        while len(out) < limit:
            if next_token:
                params["next_token"] = next_token
            data = await self.http.get(
                f"{_BASE}/tweets/search/recent",
                bucket="xapi",
                params=params,
                headers={"Authorization": f"Bearer {self.bearer}"},
            )
            if not isinstance(data, dict) or not data.get("data"):
                break
            users = {u["id"]: u for u in (data.get("includes", {}).get("users") or [])}
            for t in data["data"]:
                rt = _normalise(t, users)
                if rt:
                    out.append(rt)
            next_token = (data.get("meta") or {}).get("next_token")
            if not next_token:
                break
        return out[:limit]

    async def search(self, query: str, since: datetime, limit: int = 200) -> AsyncIterator[RawTweet]:
        for t in await self._search(query, since, limit):
            yield t

    async def user_timeline(self, handle: str, since: datetime, limit: int = 100) -> AsyncIterator[RawTweet]:
        for t in await self._search(f"from:{handle}", since, limit):
            yield t


def _normalise(t: dict, users: dict) -> RawTweet | None:
    u = users.get(t.get("author_id", ""), {})
    handle = str(u.get("username", "")).lower()
    if not handle:
        return None
    try:
        posted = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None
    pm = t.get("public_metrics") or {}
    upm = u.get("public_metrics") or {}
    refs = {r.get("type") for r in (t.get("referenced_tweets") or [])}
    urls = [
        e["expanded_url"]
        for e in ((t.get("entities") or {}).get("urls") or [])
        if e.get("expanded_url")
    ]
    return RawTweet(
        tweet_id=str(t["id"]),
        handle=handle,
        posted_at=posted,
        text=t.get("text", ""),
        url=f"https://x.com/{handle}/status/{t['id']}",
        source="xapi",
        lang=t.get("lang"),
        platform_user_id=str(t.get("author_id") or "") or None,
        display_name=u.get("name"),
        followers=upm.get("followers_count"),
        following=upm.get("following_count"),
        like_count=pm.get("like_count"),
        retweet_count=pm.get("retweet_count"),
        reply_count=pm.get("reply_count"),
        view_count=pm.get("impression_count"),
        is_retweet="retweeted" in refs,
        is_reply="replied_to" in refs,
        is_quote="quoted" in refs,
        expanded_urls=urls,
    )
