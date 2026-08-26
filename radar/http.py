"""Bedava API'ler icin ince HTTP katmani.

Iki sey yapar, fazlasini degil:
  * kaynak basina token-bucket (429 yiyip ban olmamak icin)
  * 429/5xx'te ustel geri cekilme

Her cagri hata firlatmaz -- None doner. Bedava kaynaklar ara sira duser ve
bir kaynagin dusmesi botun durmasi anlamina gelmemeli.
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
    """Token-bucket. `rate` saniyede izin verilen istek sayisi."""

    def __init__(self, rate: float) -> None:
        self.rate = max(0.01, rate)
        self.capacity = max(1.0, rate)
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
_RATES = {
    "geckoterminal": lambda: settings.rate_geckoterminal,
    "dexscreener": lambda: settings.rate_dexscreener,
    "rugcheck": lambda: settings.rate_rugcheck,
    "goplus": lambda: settings.rate_goplus,
    "telegram": lambda: settings.rate_telegram,
}


def limiter_for(bucket: str) -> RateLimiter:
    if bucket not in _LIMITERS:
        _LIMITERS[bucket] = RateLimiter(_RATES.get(bucket, lambda: 2.0)())
    return _LIMITERS[bucket]


class HttpClient:
    """Tek bir httpx.AsyncClient'i paylasan sarmalayici.

    `async with HttpClient() as http:` seklinde kullanilir.
    """

    def __init__(self, timeout: float | None = None) -> None:
        self._timeout = timeout or settings.http_timeout
        self._client: httpx.AsyncClient | None = None
        # Son ham yanit — diag komutu bunu gosterir.
        self.last_error: str | None = None

    async def __aenter__(self) -> HttpClient:
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers={
                "Accept": "application/json",
                "User-Agent": "radar-bot/1.0 (+https://github.com)",
            },
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("HttpClient 'async with' blogu icinde kullanilmali")
        return self._client

    async def request(
        self,
        method: str,
        url: str,
        *,
        bucket: str = "default",
        params: dict | None = None,
        json_body: Any = None,
        headers: dict | None = None,
    ) -> Any | None:
        """JSON doner; kalici hata durumunda None."""
        attempts = max(1, settings.http_max_retries)
        for i in range(attempts):
            await limiter_for(bucket).acquire()
            try:
                r = await self.client.request(
                    method, url, params=params, json=json_body, headers=headers
                )
            except (httpx.HTTPError, OSError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.debug("%s istek hatasi (%s/%s): %s", bucket, i + 1, attempts, exc)
                await self._backoff(i)
                continue

            if r.status_code == 429 or 500 <= r.status_code < 600:
                self.last_error = f"HTTP {r.status_code}"
                retry_after = r.headers.get("retry-after")
                if retry_after and retry_after.isdigit():
                    await asyncio.sleep(min(60.0, float(retry_after)))
                else:
                    await self._backoff(i)
                continue

            if r.status_code >= 400:
                # 404 = token yok; bu bir hata degil, cevaptir.
                self.last_error = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code != 404:
                    log.debug("%s %s -> %s", bucket, url, self.last_error)
                return None

            self.last_error = None
            if not r.content:
                return None
            try:
                return r.json()
            except ValueError:
                self.last_error = f"JSON degil: {r.text[:200]}"
                return None
        return None

    async def get(self, url: str, *, bucket: str = "default", params: dict | None = None,
                  headers: dict | None = None) -> Any | None:
        return await self.request("GET", url, bucket=bucket, params=params, headers=headers)

    async def post(self, url: str, *, bucket: str = "default", json_body: Any = None,
                   params: dict | None = None) -> Any | None:
        return await self.request("POST", url, bucket=bucket, params=params, json_body=json_body)

    @staticmethod
    async def _backoff(attempt: int) -> None:
        await asyncio.sleep(min(20.0, (2 ** attempt) + random.random()))
