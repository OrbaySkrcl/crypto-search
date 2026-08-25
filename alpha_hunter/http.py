"""Ortak async HTTP katmani: token-bucket rate limit + ustel geri cekilme.

Bedava API'ler (DexScreener, GeckoTerminal) sinirlari asinca 429 doner ve
bir sure ban yer. Her kaynak icin ayri kova tutuyoruz.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)


class RateLimiter:
    """Basit token-bucket. `rate` = saniyedeki izin verilen istek sayisi."""

    def __init__(self, rate: float, burst: float | None = None) -> None:
        self.rate = max(0.01, rate)
        self.capacity = burst if burst is not None else max(1.0, rate)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self.rate)


_LIMITERS: dict[str, RateLimiter] = {}


def limiter_for(name: str) -> RateLimiter:
    if name not in _LIMITERS:
        rate = {
            "dexscreener": settings.rate_dexscreener,
            "geckoterminal": settings.rate_geckoterminal,
            "birdeye": settings.rate_birdeye,
            "rugcheck": settings.rate_rugcheck,
            "rpc": settings.rate_rpc,
        }.get(name, 2.0)
        _LIMITERS[name] = RateLimiter(rate)
    return _LIMITERS[name]


class HttpClient:
    """Tek bir httpx.AsyncClient'i paylasan ince sarmalayici."""

    def __init__(self, timeout: float | None = None, headers: dict[str, str] | None = None) -> None:
        self._client: httpx.AsyncClient | None = None
        self._timeout = timeout or settings.http_timeout
        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
            "Accept": "application/json, text/xml, */*",
            **(headers or {}),
        }

    async def __aenter__(self) -> HttpClient:
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers=self._headers,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("HttpClient 'async with' icinde kullanilmali")
        return self._client

    async def get(
        self,
        url: str,
        *,
        bucket: str = "default",
        params: dict | None = None,
        headers: dict | None = None,
        expect_json: bool = True,
        max_retries: int | None = None,
    ) -> Any | None:
        retries = max_retries if max_retries is not None else settings.http_max_retries
        lim = limiter_for(bucket)

        for attempt in range(retries + 1):
            await lim.acquire()
            try:
                r = await self.client.get(url, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= retries:
                    log.debug("GET %s agi hatasi: %s", url, exc)
                    return None
                await _backoff(attempt)
                continue

            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 0) or 0) or (2 ** attempt)
                log.debug("429 %s -> %.1fs bekle", url, wait)
                await asyncio.sleep(min(wait, 60) + random.random())
                continue
            if r.status_code in (500, 502, 503, 504):
                if attempt >= retries:
                    return None
                await _backoff(attempt)
                continue
            if r.status_code == 404:
                return None
            if r.status_code >= 400:
                log.debug("GET %s -> HTTP %s", url, r.status_code)
                return None

            if not expect_json:
                return r.text
            try:
                return r.json()
            except Exception:
                log.debug("GET %s JSON parse edilemedi", url)
                return None
        return None

    async def post(
        self,
        url: str,
        *,
        bucket: str = "default",
        json_body: dict | None = None,
        headers: dict | None = None,
        max_retries: int | None = None,
    ) -> Any | None:
        retries = max_retries if max_retries is not None else settings.http_max_retries
        lim = limiter_for(bucket)
        for attempt in range(retries + 1):
            await lim.acquire()
            try:
                r = await self.client.post(url, json=json_body, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt >= retries:
                    return None
                await _backoff(attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt >= retries:
                    return None
                await _backoff(attempt)
                continue
            if r.status_code >= 400:
                log.debug("POST %s -> HTTP %s: %s", url, r.status_code, r.text[:200])
                return None
            try:
                return r.json()
            except Exception:
                return None
        return None


async def _backoff(attempt: int) -> None:
    await asyncio.sleep(min(2 ** attempt, 30) + random.random())
