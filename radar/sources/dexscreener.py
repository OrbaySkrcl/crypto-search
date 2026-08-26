"""DexScreener — BEDAVA, anahtarsiz, toplu sorgu.

Tek cagrida 30 tokenin fiyat/likidite/hacim bilgisini verir. Fiyat
ornekleme dongusunun tamami buradan besleniyor; bu yuzden 300 token
izlemek 10 istek eder, 300 degil.

GeckoTerminal ile ayni veriyi verir ama BATCH oldugu icin kota dostudur.
Ikisi birbirinin capraz dogrulamasidir: fiyat ikisinde de cok farkliysa
kart uretilmez (yanlis havuz korumasi).
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from ..http import HttpClient

log = logging.getLogger(__name__)

BASE = "https://api.dexscreener.com"
BUCKET = "dexscreener"
CHUNK = 30

# DexScreener zincir kimlikleri bizimkilerle ayni; istisnalar burada.
CHAIN_ALIAS = {"polygon": "polygon", "bsc": "bsc", "ethereum": "ethereum"}


def _f(v) -> float | None:
    try:
        if v in (None, "", "N/A"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class TokenSnapshot:
    chain: str
    address: str
    symbol: str | None
    name: str | None
    pair_address: str | None
    dex_id: str | None
    price_usd: float | None
    mc_usd: float | None
    liquidity_usd: float | None
    volume_h1: float | None
    volume_h24: float | None
    price_change_h1: float | None
    price_change_h24: float | None
    buys_h1: int | None
    sells_h1: int | None
    pair_created_at: datetime | None

    @property
    def buy_pressure(self) -> float | None:
        """1 saatlik alim/satim dengesi. 0.5 = notr."""
        b, s = self.buys_h1 or 0, self.sells_h1 or 0
        return b / (b + s) if (b + s) >= 10 else None


class DexScreener:
    def __init__(self, http: HttpClient, base: str = BASE) -> None:
        self.http = http
        self.base = base.rstrip("/")

    def _parse_pair(self, p: dict) -> TokenSnapshot | None:
        if not isinstance(p, dict):
            return None
        base_tok = p.get("baseToken") or {}
        addr = base_tok.get("address")
        if not addr:
            return None
        created = p.get("pairCreatedAt")
        try:
            created_at = (
                datetime.fromtimestamp(float(created) / 1000, tz=timezone.utc) if created else None
            )
        except (TypeError, ValueError, OSError):
            created_at = None
        return TokenSnapshot(
            chain=str(p.get("chainId") or "").lower(),
            address=str(addr),
            symbol=base_tok.get("symbol"),
            name=base_tok.get("name"),
            pair_address=p.get("pairAddress"),
            dex_id=p.get("dexId"),
            price_usd=_f(p.get("priceUsd")),
            mc_usd=_f(p.get("marketCap")) or _f(p.get("fdv")),
            liquidity_usd=_f((p.get("liquidity") or {}).get("usd")),
            volume_h1=_f((p.get("volume") or {}).get("h1")),
            volume_h24=_f((p.get("volume") or {}).get("h24")),
            price_change_h1=_f((p.get("priceChange") or {}).get("h1")),
            price_change_h24=_f((p.get("priceChange") or {}).get("h24")),
            buys_h1=((p.get("txns") or {}).get("h1") or {}).get("buys"),
            sells_h1=((p.get("txns") or {}).get("h1") or {}).get("sells"),
            pair_created_at=created_at,
        )

    async def tokens(
        self, addresses: Sequence[str], chain: str | None = None
    ) -> dict[str, TokenSnapshot]:
        """Adres -> en likit havuz bilgisi. Adresler 30'luk gruplara bolunur."""
        out: dict[str, TokenSnapshot] = {}
        uniq = list(dict.fromkeys(a for a in addresses if a))
        for i in range(0, len(uniq), CHUNK):
            chunk = uniq[i : i + CHUNK]
            data = await self.http.get(
                f"{self.base}/latest/dex/tokens/{','.join(chunk)}", bucket=BUCKET
            )
            for p in (data or {}).get("pairs") or []:
                snap = self._parse_pair(p)
                if snap is None:
                    continue
                if chain and snap.chain != CHAIN_ALIAS.get(chain, chain):
                    continue
                prev = out.get(snap.address)
                # Ayni token birden cok havuzda: EN LIKIT olani esas al.
                if prev is None or (snap.liquidity_usd or 0) > (prev.liquidity_usd or 0):
                    out[snap.address] = snap
        return out

    async def token(self, address: str, chain: str | None = None) -> TokenSnapshot | None:
        return (await self.tokens([address], chain)).get(address)

    async def search(self, query: str) -> list[TokenSnapshot]:
        data = await self.http.get(
            f"{self.base}/latest/dex/search", bucket=BUCKET, params={"q": query}
        )
        out = [self._parse_pair(p) for p in (data or {}).get("pairs") or []]
        return [s for s in out if s]

    async def diagnose(self) -> dict:
        # USDC — her zaman var olan bir referans.
        ref = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        data = await self.http.get(f"{self.base}/latest/dex/tokens/{ref}", bucket=BUCKET)
        pairs = (data or {}).get("pairs") or []
        return {
            "kaynak": "dexscreener",
            "ok": bool(pairs),
            "havuz": len(pairs),
            "hata": self.http.last_error,
            "ornek": (pairs[0] if pairs else None),
        }
