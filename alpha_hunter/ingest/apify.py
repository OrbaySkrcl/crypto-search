"""Apify aktor kaynagi -- odemeli ama en guvenilir yol.

Nitter dustugunde/veri eksik geldiginde bu devreye girer. Aktor ciktisi
aktorden aktore degistigi icin normalizer esnek yazildi.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from ..config import settings
from ..http import HttpClient
from .base import RawTweet

log = logging.getLogger(__name__)
_BASE = "https://api.apify.com/v2"


class ApifySource:
    name = "apify"

    def __init__(self, http: HttpClient, token: str | None = None, actor: str | None = None) -> None:
        self.http = http
        self.token = token or settings.apify_token
        self.actor = (actor or settings.apify_actor).replace("/", "~")

    async def available(self) -> bool:
        return bool(self.token)

    async def _run(self, payload: dict, limit: int) -> list[dict]:
        if not self.token:
            log.warning("APIFY_TOKEN yok, apify atlaniyor")
            return []
        url = f"{_BASE}/acts/{self.actor}/run-sync-get-dataset-items"
        data = await self.http.post(
            url,
            bucket="apify",
            json_body=payload,
            headers={"Authorization": f"Bearer {self.token}"},
            max_retries=2,
        )
        if isinstance(data, list):
            if not data:
                log.warning(
                    "apify aktoru bos liste dondu (aktor: %s). Kredi bitmis, aktor adi "
                    "yanlis ya da girdi bicimi uyumsuz olabilir.", self.actor
                )
            return data[:limit]
        if isinstance(data, dict):
            if isinstance(data.get("items"), list):
                return data["items"][:limit]
            # Apify hata govdesi: {"error": {"type": ..., "message": ...}}
            err = data.get("error") or {}
            log.error(
                "apify hatasi (aktor: %s): %s %s",
                self.actor, err.get("type", "?"), str(err.get("message", data))[:220],
            )
            return []
        log.error(
            "apify yanit vermedi (aktor: %s). Token gecerli mi, aktor adi dogru mu?",
            self.actor,
        )
        return []

    async def search(self, query: str, since: datetime, limit: int = 200) -> AsyncIterator[RawTweet]:
        payload = {
            "searchTerms": [query],
            "maxItems": limit,
            "sort": "Latest",
            "start": since.strftime("%Y-%m-%d_%H:%M:%S_UTC"),
            "includeSearchTerms": False,
        }
        for item in await self._run(payload, limit):
            t = _normalise(item)
            if t and t.posted_at >= since:
                yield t

    async def user_timeline(self, handle: str, since: datetime, limit: int = 100) -> AsyncIterator[RawTweet]:
        payload = {
            "twitterHandles": [handle],
            "maxItems": limit,
            "sort": "Latest",
            "start": since.strftime("%Y-%m-%d_%H:%M:%S_UTC"),
        }
        for item in await self._run(payload, limit):
            t = _normalise(item)
            if t and t.posted_at >= since:
                yield t


# --------------------------------------------------------------------------- #
def _pick(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / (1000 if v > 1e11 else 1), tz=timezone.utc)
    s = str(v).strip()
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc) if "%z" not in fmt \
                else datetime.strptime(s, fmt).astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _normalise(item: dict, source: str = "apify") -> RawTweet | None:
    if not isinstance(item, dict) or item.get("noResults"):
        return None

    author = item.get("author") or item.get("user") or {}
    handle = str(
        _pick(item, "username", "userName", "screen_name", default=None)
        or _pick(author, "userName", "username", "screen_name", default="")
    ).lstrip("@").lower()

    tweet_id = str(_pick(item, "id", "id_str", "tweetId", "rest_id", default="") or "")
    url = _pick(item, "url", "twitterUrl", "tweetUrl")
    if not tweet_id and url:
        tweet_id = str(url).rstrip("/").split("/")[-1].split("?")[0]
    if not tweet_id or not handle:
        return None

    posted = _parse_dt(_pick(item, "createdAt", "created_at", "date", "timestamp"))
    if posted is None:
        return None

    text = str(_pick(item, "fullText", "full_text", "text", "content", default="") or "")

    urls: list[str] = []
    ent = item.get("entities") or {}
    for u in (ent.get("urls") or []):
        if isinstance(u, dict) and u.get("expanded_url"):
            urls.append(u["expanded_url"])
    for u in (item.get("urls") or []):
        if isinstance(u, str):
            urls.append(u)
        elif isinstance(u, dict) and u.get("expanded_url"):
            urls.append(u["expanded_url"])

    return RawTweet(
        tweet_id=tweet_id,
        handle=handle,
        posted_at=posted,
        text=text,
        url=str(url) if url else f"https://x.com/{handle}/status/{tweet_id}",
        source=source,
        lang=_pick(item, "lang", "language"),
        platform_user_id=str(_pick(author, "id", "id_str", "rest_id", default="") or "") or None,
        display_name=_pick(author, "name", "displayName"),
        followers=_pick(author, "followers", "followersCount", "followers_count"),
        following=_pick(author, "following", "followingCount", "friends_count"),
        account_created_at=_parse_dt(_pick(author, "createdAt", "created_at")),
        like_count=_pick(item, "likeCount", "favorite_count", "favoriteCount"),
        retweet_count=_pick(item, "retweetCount", "retweet_count"),
        reply_count=_pick(item, "replyCount", "reply_count"),
        view_count=_pick(item, "viewCount", "views"),
        is_retweet=bool(_pick(item, "isRetweet", "retweeted", default=False)),
        is_reply=bool(_pick(item, "isReply", default=False)) or bool(item.get("inReplyToId")),
        is_quote=bool(_pick(item, "isQuote", "is_quote_status", default=False)),
        expanded_urls=list(dict.fromkeys(urls)),
        raw=None,
    )
