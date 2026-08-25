"""Birdeye OHLCV -- API anahtari gerekir, ama en hassas ve genis kapsamli.

Anahtar varsa GeckoTerminal yerine bu tercih edilir: 1 dakikalik mumlari
uzun araliklarda dondurebiliyor ve rate limiti cok daha rahat.
"""
from __future__ import annotations

import logging
from datetime import datetime

from ..config import settings
from ..http import HttpClient
from .types import Candle, utc

log = logging.getLogger(__name__)

CHAIN_HEADER = {
    "solana": "solana",
    "ethereum": "ethereum",
    "base": "base",
    "bsc": "bsc",
    "arbitrum": "arbitrum",
    "polygon": "polygon",
}
TYPES = {"1m": ("1m", 60), "5m": ("5m", 300), "15m": ("15m", 900), "1h": ("1H", 3600), "1d": ("1D", 86400)}


class BirdeyeClient:
    def __init__(self, http: HttpClient, api_key: str | None = None) -> None:
        self.http = http
        self.key = api_key or settings.birdeye_api_key
        self.base = settings.birdeye_base.rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def _headers(self, chain: str) -> dict[str, str]:
        return {
            "X-API-KEY": self.key or "",
            "x-chain": CHAIN_HEADER.get(chain, "solana"),
            "Accept": "application/json",
        }

    async def ohlcv(
        self, chain: str, token_address: str, start: datetime, end: datetime, resolution: str = "1m"
    ) -> list[Candle]:
        if not self.enabled or resolution not in TYPES:
            return []
        rtype, secs = TYPES[resolution]
        data = await self.http.get(
            f"{self.base}/defi/ohlcv",
            bucket="birdeye",
            params={
                "address": token_address,
                "type": rtype,
                "time_from": str(int(start.timestamp())),
                "time_to": str(int(end.timestamp())),
            },
            headers=self._headers(chain),
        )
        items = ((data or {}).get("data") or {}).get("items") or []
        out: list[Candle] = []
        for it in items:
            try:
                out.append(
                    Candle(
                        utc(it["unixTime"]),
                        float(it["o"]), float(it["h"]), float(it["l"]), float(it["c"]),
                        float(it.get("v") or 0), secs,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda c: c.ts)
        return out

    async def price_at(self, chain: str, token_address: str, ts: datetime) -> float | None:
        """Tek noktada tarihsel fiyat (history_price ucu)."""
        if not self.enabled:
            return None
        data = await self.http.get(
            f"{self.base}/defi/history_price",
            bucket="birdeye",
            params={
                "address": token_address,
                "address_type": "token",
                "type": "1m",
                "time_from": str(int(ts.timestamp()) - 120),
                "time_to": str(int(ts.timestamp()) + 60),
            },
            headers=self._headers(chain),
        )
        items = ((data or {}).get("data") or {}).get("items") or []
        if not items:
            return None
        target = ts.timestamp()
        best = min(items, key=lambda i: abs(float(i.get("unixTime", 0)) - target))
        try:
            return float(best["value"])
        except (KeyError, TypeError, ValueError):
            return None

    async def token_overview(self, chain: str, token_address: str) -> dict | None:
        if not self.enabled:
            return None
        data = await self.http.get(
            f"{self.base}/defi/token_overview",
            bucket="birdeye",
            params={"address": token_address},
            headers=self._headers(chain),
        )
        return (data or {}).get("data")
