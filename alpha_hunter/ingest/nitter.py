"""Nitter RSS kaynagi -- API anahtari YOK, ucretsiz.

Twitter API limitlerini bypass etmenin en ucuz yolu. Dezavantaji: nitter
ornekleri (instance) surekli duser. Bu yuzden havuz + saglik takibi var:
basarisiz olan ornek gecici olarak cezalandirilir, siradaki denenir.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from xml.etree import ElementTree as ET

from ..config import settings
from ..http import HttpClient
from .base import RawTweet

log = logging.getLogger(__name__)

_NS = {"dc": "http://purl.org/dc/elements/1.1/"}
_TAG_RE = re.compile(r"<[^>]+>")
_STATUS_RE = re.compile(r"/status/(\d+)")
_COOLDOWN_SEC = 600.0


class NitterSource:
    name = "nitter"

    def __init__(self, http: HttpClient, instances: list[str] | None = None) -> None:
        self.http = http
        self.instances = instances or settings.nitter_list
        self._penalty: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    def _ordered(self) -> list[str]:
        now = time.monotonic()
        healthy = [i for i in self.instances if self._penalty.get(i, 0) < now]
        return healthy or list(self.instances)

    def _punish(self, inst: str) -> None:
        self._penalty[inst] = time.monotonic() + _COOLDOWN_SEC

    async def _fetch_rss(self, path: str) -> str | None:
        for inst in self._ordered():
            body = await self.http.get(
                f"{inst}{path}",
                bucket="nitter",
                expect_json=False,
                max_retries=1,
                headers={"Accept": "application/rss+xml, text/xml"},
            )
            if body and body.lstrip().startswith("<?xml"):
                return body
            self._punish(inst)
            log.debug("nitter ornegi basarisiz: %s", inst)
        return None

    # ------------------------------------------------------------------ #
    async def search(self, query: str, since: datetime, limit: int = 200) -> AsyncIterator[RawTweet]:
        path = f"/search/rss?f=tweets&q={quote(query)}"
        body = await self._fetch_rss(path)
        if not body:
            log.warning("nitter: '%s' icin hicbir ornek yanit vermedi", query[:48])
            return
        n = 0
        for t in _parse_rss(body, self.name):
            if t.posted_at < since:
                continue
            yield t
            n += 1
            if n >= limit:
                return

    async def user_timeline(self, handle: str, since: datetime, limit: int = 100) -> AsyncIterator[RawTweet]:
        body = await self._fetch_rss(f"/{quote(handle)}/rss")
        if not body:
            return
        n = 0
        for t in _parse_rss(body, self.name, force_handle=handle):
            if t.posted_at < since:
                continue
            yield t
            n += 1
            if n >= limit:
                return

    async def available(self) -> bool:
        return bool(self.instances)


# --------------------------------------------------------------------------- #
def _parse_rss(body: str, source: str, force_handle: str | None = None) -> list[RawTweet]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []

    out: list[RawTweet] = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        m = _STATUS_RE.search(link)
        if not m:
            continue
        tweet_id = m.group(1)

        creator = (item.findtext("dc:creator", namespaces=_NS) or "").strip().lstrip("@")
        handle = (creator or force_handle or "").lower()
        if not handle:
            continue

        pub = item.findtext("pubDate")
        try:
            posted = parsedate_to_datetime(pub).astimezone(timezone.utc) if pub else None
        except Exception:
            posted = None
        if posted is None:
            continue

        desc = item.findtext("description") or ""
        title = item.findtext("title") or ""
        # RSS description'daki linkler zaten genisletilmis halde gelir
        expanded = re.findall(r'href="(https?://[^"]+)"', desc)
        text = _TAG_RE.sub(" ", desc)
        text = _unescape(text).strip() or _unescape(title).strip()

        out.append(
            RawTweet(
                tweet_id=tweet_id,
                handle=handle,
                posted_at=posted,
                text=text,
                url=f"https://x.com/{handle}/status/{tweet_id}",
                source=source,
                is_retweet=title.strip().startswith("RT by") or "RT @" in title,
                expanded_urls=[u for u in expanded if "nitter" not in u and "/pic/" not in u],
            )
        )
    return out


_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": " "}


def _unescape(s: str) -> str:
    for k, v in _ENTITIES.items():
        s = s.replace(k, v)
    return re.sub(r"\s+", " ", s)
