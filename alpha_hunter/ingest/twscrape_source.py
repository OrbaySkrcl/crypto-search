"""twscrape kaynagi (opsiyonel).

Kendi X hesaplarinla giris yapip GraphQL uclarini kullanir -- en zengin veri,
ama hesap yasaklanma riski var. Kutuphane kurulu degilse sessizce devre disi.
Kurulum:  pip install twscrape
Hesap ekleme: twscrape add_accounts accounts.txt username:password:email:email_password
              twscrape login_accounts
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from ..config import settings
from .base import RawTweet

log = logging.getLogger(__name__)


class TwscrapeSource:
    name = "twscrape"

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or settings.twscrape_db
        self._api: Any = None
        self.last_detail: str | None = None

    async def available(self) -> bool:
        try:
            from twscrape import API  # noqa: F401
        except ImportError:
            return False
        return True

    async def _get_api(self) -> Any:
        if self._api is None:
            from twscrape import API
            self._api = API(self.db_path)
        return self._api

    async def search(self, query: str, since: datetime, limit: int = 200) -> AsyncIterator[RawTweet]:
        if not await self.available():
            return
        api = await self._get_api()
        n = 0
        async for tw in api.search(query, limit=limit):
            rt = _normalise(tw)
            if rt and rt.posted_at >= since:
                yield rt
                n += 1
                if n >= limit:
                    return

    async def user_timeline(self, handle: str, since: datetime, limit: int = 100) -> AsyncIterator[RawTweet]:
        if not await self.available():
            return
        api = await self._get_api()
        user = await api.user_by_login(handle)
        if not user:
            return
        n = 0
        async for tw in api.user_tweets(user.id, limit=limit):
            rt = _normalise(tw)
            if rt and rt.posted_at >= since:
                yield rt
                n += 1
                if n >= limit:
                    return


def _normalise(tw: Any) -> RawTweet | None:
    try:
        user = tw.user
        posted = tw.date
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return RawTweet(
            tweet_id=str(tw.id),
            handle=str(user.username).lower(),
            posted_at=posted.astimezone(timezone.utc),
            text=getattr(tw, "rawContent", "") or "",
            url=getattr(tw, "url", None),
            source="twscrape",
            lang=getattr(tw, "lang", None),
            platform_user_id=str(user.id),
            display_name=getattr(user, "displayname", None),
            followers=getattr(user, "followersCount", None),
            following=getattr(user, "friendsCount", None),
            account_created_at=getattr(user, "created", None),
            like_count=getattr(tw, "likeCount", None),
            retweet_count=getattr(tw, "retweetCount", None),
            reply_count=getattr(tw, "replyCount", None),
            view_count=getattr(tw, "viewCount", None),
            is_retweet=getattr(tw, "retweetedTweet", None) is not None,
            is_reply=getattr(tw, "inReplyToTweetId", None) is not None,
            is_quote=getattr(tw, "quotedTweet", None) is not None,
            expanded_urls=[ln.url for ln in (getattr(tw, "links", None) or []) if getattr(ln, "url", None)],
        )
    except Exception as exc:  # pragma: no cover
        log.debug("twscrape normalize hatasi: %s", exc)
        return None
