"""DexScreener istemcisi -- anahtar gerektirmez, coklu zincir.

Rolu: token dogrulama + ana likidite havuzunu bulma + ANLIK fiyat/MC/likidite.
Gecmis fiyat vermez; onu GeckoTerminal/Birdeye saglar.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone

from ..config import settings
from ..http import HttpClient
from .types import TokenInfo

log = logging.getLogger(__name__)

CHAIN_IDS = {
    "solana": "solana",
    "ethereum": "ethereum",
    "base": "base",
    "bsc": "bsc",
    "arbitrum": "arbitrum",
    "polygon": "polygon",
}
STABLE_QUOTES = {"USDC", "USDT", "SOL", "WSOL", "WETH", "ETH", "WBNB", "BNB", "DAI"}


class DexScreenerClient:
    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self.base = settings.dexscreener_base.rstrip("/")

    # ------------------------------------------------------------------ #
    async def tokens(self, addresses: Sequence[str], chain: str | None = None) -> dict[str, TokenInfo]:
        """30'ar adresi tek istekte cozer. Anahtar: kucuk harf adres."""
        out: dict[str, TokenInfo] = {}
        addrs = list(dict.fromkeys(a for a in addresses if a))
        for i in range(0, len(addrs), 30):
            chunk = addrs[i : i + 30]
            data = await self.http.get(
                f"{self.base}/latest/dex/tokens/{','.join(chunk)}", bucket="dexscreener"
            )
            pairs = _pairs_of(data)
            if not pairs:
                continue
            for addr in chunk:
                info = self._best_for(addr, pairs, chain)
                if info:
                    out[addr.lower()] = info
        return out

    async def token(self, address: str, chain: str | None = None) -> TokenInfo | None:
        got = await self.tokens([address], chain)
        return got.get(address.lower())

    async def pair(self, chain: str, pair_address: str) -> TokenInfo | None:
        cid = CHAIN_IDS.get(chain, chain)
        data = await self.http.get(
            f"{self.base}/latest/dex/pairs/{cid}/{pair_address}", bucket="dexscreener"
        )
        pairs = _pairs_of(data)
        if not pairs:
            return None
        return _to_info(pairs[0])

    async def resolve_pair_or_token(self, chain: str, address: str) -> TokenInfo | None:
        """Kullanicilar bazen PAIR adresi paylasir (dexscreener linki).
        Once token olarak dener, olmazsa pair olarak cozer."""
        info = await self.token(address, chain)
        if info:
            return info
        return await self.pair(chain, address)

    # ------------------------------------------------------------------ #
    def _best_for(self, address: str, pairs: Iterable[dict], chain: str | None) -> TokenInfo | None:
        """En yuksek likiditeye sahip, adresin BASE token oldugu havuzu sec."""
        cid = CHAIN_IDS.get(chain or "", None)
        cands: list[tuple[float, dict]] = []
        for p in pairs:
            base = (p.get("baseToken") or {}).get("address") or ""
            if base.lower() != address.lower():
                continue
            if cid and p.get("chainId") != cid:
                continue
            liq = float(((p.get("liquidity") or {}).get("usd") or 0) or 0)
            quote_sym = ((p.get("quoteToken") or {}).get("symbol") or "").upper()
            # Stabil/ana coin karsiligi olan havuzlari tercih et
            bonus = 1.15 if quote_sym in STABLE_QUOTES else 1.0
            cands.append((liq * bonus, p))
        if not cands:
            return None
        cands.sort(key=lambda t: t[0], reverse=True)
        return _to_info(cands[0][1])


# --------------------------------------------------------------------------- #
def _pairs_of(data) -> list[dict]:
    if isinstance(data, dict):
        for key in ("pairs", "data"):
            v = data.get(key)
            if isinstance(v, list):
                return v
    if isinstance(data, list):
        return data
    return []


def _f(v) -> float | None:
    try:
        if v in (None, "", "N/A"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_info(p: dict) -> TokenInfo:
    base = p.get("baseToken") or {}
    price = _f(p.get("priceUsd"))
    mc = _f(p.get("marketCap"))
    fdv = _f(p.get("fdv"))
    created = p.get("pairCreatedAt")
    created_dt = (
        datetime.fromtimestamp(created / 1000, tz=timezone.utc)
        if isinstance(created, (int, float)) and created > 0
        else None
    )
    supply = None
    ref_mc = mc or fdv
    if price and price > 0 and ref_mc:
        supply = ref_mc / price

    txns = p.get("txns") or {}
    h24 = txns.get("h24") or {}
    tx24 = None
    if isinstance(h24, dict):
        tx24 = int((h24.get("buys") or 0) + (h24.get("sells") or 0))

    chain_raw = str(p.get("chainId") or "").lower()
    chain = next((k for k, v in CHAIN_IDS.items() if v == chain_raw), chain_raw)

    return TokenInfo(
        chain=chain,
        address=base.get("address") or "",
        symbol=base.get("symbol"),
        name=base.get("name"),
        pair_address=p.get("pairAddress"),
        dex_id=p.get("dexId"),
        pair_created_at=created_dt,
        price_usd=price,
        mc_usd=mc or fdv,
        fdv_usd=fdv,
        liquidity_usd=_f((p.get("liquidity") or {}).get("usd")),
        volume_24h=_f((p.get("volume") or {}).get("h24")),
        supply_estimate=supply,
        txns_24h=tx24,
    )
