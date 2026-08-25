"""Kaynak orkestrasyonu: birden fazla kaziyiciyi sirayla dener, tekillestirir."""
from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime

from ..config import settings
from ..http import HttpClient
from .apify import ApifySource
from .base import RawTweet, TweetSource
from .nitter import NitterSource
from .twscrape_source import TwscrapeSource
from .xapi import XApiSource

log = logging.getLogger(__name__)


def build_sources(http: HttpClient, names: Sequence[str] | None = None) -> list[TweetSource]:
    names = list(names or settings.source_list)
    built: list[TweetSource] = []
    for n in names:
        if n == "nitter":
            built.append(NitterSource(http))
        elif n == "apify":
            built.append(ApifySource(http))
        elif n == "xapi":
            built.append(XApiSource(http))
        elif n == "twscrape":
            built.append(TwscrapeSource())
        else:
            log.warning("bilinmeyen tweet kaynagi: %s", n)
    return built


class Collector:
    """Kaynaklari oncelik sirasiyla dener; bir kaynak veri dondurduyse
    digerlerine gecmeden once onu kullanir (maliyet ve limit tasarrufu)."""

    def __init__(self, sources: Sequence[TweetSource], fallback_chain: bool = True) -> None:
        self.sources = list(sources)
        self.fallback_chain = fallback_chain
        self._reported = False

    async def collect_search(
        self, queries: Sequence[str], since: datetime, limit_per_query: int = 200
    ) -> list[RawTweet]:
        seen: dict[str, RawTweet] = {}
        await self._report_sources()
        for q in queries:
            got_any = False
            for src in self.sources:
                if not await src.available():
                    continue
                try:
                    count = 0
                    async for t in src.search(q, since, limit_per_query):
                        seen.setdefault(t.tweet_id, t)
                        count += 1
                    if count:
                        got_any = True
                        log.info("[%s] '%s' -> %d tweet", src.name, q[:40], count)
                    else:
                        # Sessiz sifir, hatadan daha sinsi: kaynak calisti ama
                        # bos dondu. Gorunur olmali, yoksa teshis imkansiz.
                        log.warning("[%s] '%s' -> 0 tweet", src.name, q[:40])
                except Exception as exc:
                    log.warning("[%s] '%s' hata: %s: %s", src.name, q[:40], type(exc).__name__, exc)
                if got_any and self.fallback_chain:
                    break
            if not got_any:
                log.warning("hicbir kaynak '%s' icin veri dondurmedi", q[:40])
        return list(seen.values())

    async def _report_sources(self) -> None:
        """Hangi kaynagin acik, hangisinin kapali oldugunu bir kez yazar."""
        if self._reported:
            return
        self._reported = True
        active, idle = [], []
        for src in self.sources:
            (active if await src.available() else idle).append(src.name)
        log.info(
            "tweet kaynaklari -> aktif: %s | kapali: %s",
            ", ".join(active) or "YOK",
            ", ".join(idle) or "-",
        )
        if not active:
            log.error(
                "Hicbir tweet kaynagi aktif degil. APIFY_TOKEN girildi mi, "
                "TWEET_SOURCES dogru mu?"
            )

    async def collect_timelines(
        self, handles: Sequence[str], since: datetime, limit_per_handle: int = 100
    ) -> list[RawTweet]:
        seen: dict[str, RawTweet] = {}
        for h in handles:
            for src in self.sources:
                if not await src.available():
                    continue
                try:
                    count = 0
                    async for t in src.user_timeline(h, since, limit_per_handle):
                        seen.setdefault(t.tweet_id, t)
                        count += 1
                    if count:
                        log.info("[%s] @%s -> %d tweet", src.name, h, count)
                        break
                except Exception as exc:
                    log.warning("[%s] @%s hata: %s", src.name, h, exc)
        return list(seen.values())
