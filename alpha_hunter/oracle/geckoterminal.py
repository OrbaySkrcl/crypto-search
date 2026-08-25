"""GeckoTerminal OHLCV -- BEDAVA tarihsel fiyat (anahtar gerektirmez).

Sistemin en kritik ucuz parcasi: "tweet atildigi saniyedeki fiyat" bilgisini
bedavaya veren tek kaynak. Limit ~30 istek/dk, o yuzden rate limiter siki.

Cozunurluk / kapsama (limit=1000 mum):
    minute/1   -> ~16.6 saat   (giris fiyati ve korunan tepe icin)
    minute/5   -> ~83 saat     (24s/72s pencereleri)
    hour/1     -> ~41 gun      (7 gun penceresi ve tweet oncesi tepe)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..config import settings
from ..http import HttpClient
from .types import Candle, utc

log = logging.getLogger(__name__)

NETWORKS = {
    "solana": "solana",
    "ethereum": "eth",
    "base": "base",
    "bsc": "bsc",
    "arbitrum": "arbitrum",
    "polygon": "polygon_pos",
}

# (timeframe, aggregate) -> mum saniyesi
RESOLUTIONS: dict[str, tuple[str, int, int]] = {
    "1m": ("minute", 1, 60),
    "5m": ("minute", 5, 300),
    "15m": ("minute", 15, 900),
    "1h": ("hour", 1, 3600),
    "4h": ("hour", 4, 14400),
    "1d": ("day", 1, 86400),
}


class GeckoTerminalClient:
    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self.base = settings.geckoterminal_base.rstrip("/")

    async def find_pool(self, chain: str, token_address: str) -> str | None:
        net = NETWORKS.get(chain)
        if not net:
            return None
        data = await self.http.get(
            f"{self.base}/networks/{net}/tokens/{token_address}/pools",
            bucket="geckoterminal",
            params={"page": "1"},
            headers={"Accept": "application/json;version=20230302"},
        )
        items = (data or {}).get("data") or []
        best, best_liq = None, -1.0
        for it in items:
            attrs = it.get("attributes") or {}
            try:
                liq = float(attrs.get("reserve_in_usd") or 0)
            except (TypeError, ValueError):
                liq = 0.0
            addr = attrs.get("address")
            if addr and liq > best_liq:
                best, best_liq = addr, liq
        return best

    async def ohlcv(
        self,
        chain: str,
        pool_address: str,
        resolution: str,
        before: datetime | None = None,
        limit: int = 1000,
    ) -> list[Candle]:
        net = NETWORKS.get(chain)
        if not net or resolution not in RESOLUTIONS:
            return []
        timeframe, aggregate, secs = RESOLUTIONS[resolution]
        params = {
            "aggregate": str(aggregate),
            "limit": str(min(1000, max(1, limit))),
            "currency": "usd",
            "token": "base",
        }
        if before:
            params["before_timestamp"] = str(int(before.timestamp()))

        data = await self.http.get(
            f"{self.base}/networks/{net}/pools/{pool_address}/ohlcv/{timeframe}",
            bucket="geckoterminal",
            params=params,
            headers={"Accept": "application/json;version=20230302"},
        )
        rows = (((data or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        out: list[Candle] = []
        for r in rows:
            if not isinstance(r, (list, tuple)) or len(r) < 5:
                continue
            try:
                ts, o, hi, lo, c = r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4])
                v = float(r[5]) if len(r) > 5 and r[5] is not None else 0.0
            except (TypeError, ValueError):
                continue
            if hi <= 0 or c <= 0:
                continue
            out.append(Candle(utc(ts), o, hi, lo, c, v, secs))
        out.sort(key=lambda c: c.ts)
        return out

    async def range(
        self, chain: str, pool_address: str, start: datetime, end: datetime, resolution: str
    ) -> list[Candle]:
        """Verilen araligi, gerekirse sayfalayarak doldurur (geriye dogru)."""
        _, _, secs = RESOLUTIONS.get(resolution, ("minute", 1, 60))
        collected: list[Candle] = []
        cursor = end
        # Guvenlik: en fazla 6 sayfa (6000 mum)
        for _ in range(6):
            batch = await self.ohlcv(chain, pool_address, resolution, before=cursor, limit=1000)
            if not batch:
                break
            collected.extend(batch)
            oldest = batch[0].ts
            if oldest <= start:
                break
            nxt = oldest - timedelta(seconds=secs)
            if nxt >= cursor:
                break
            cursor = nxt
        return [c for c in collected if c.end > start and c.ts < end]
