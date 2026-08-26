"""GeckoTerminal v2 — BEDAVA, anahtarsiz, 30 istek/dakika.

Radar'in omurgasi burasi. Uc uc kullanilir:
  * /new_pools       — yeni dogan havuzlar (aday token akisi)
  * /trending_pools  — o an hareketli havuzlar
  * /pools/{p}/trades— son ~300 takas, CUZDAN ADRESIYLE birlikte

Son uc kritik: cuzdan bazli akilli para analizinin tamami bu tek bedava
uctan besleniyor. Birdeye/Nansen gibi aylik $100+ servislerin yerini
tutan sey bu.

Ayristiricilar bilerek toleransli: alan adlari surumler arasi degisirse
kayit dusmez, eksik alan None kalir.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import GT_NETWORK
from ..http import HttpClient

log = logging.getLogger(__name__)

BASE = "https://api.geckoterminal.com/api/v2"
BUCKET = "geckoterminal"


# --------------------------------------------------------------------------- #
#  Yardimcilar
# --------------------------------------------------------------------------- #
def _f(v) -> float | None:
    try:
        if v in (None, "", "N/A"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _dig(d, *path, default=None):
    cur = d
    for k in path:
        if isinstance(cur, dict):
            cur = cur.get(k)
        elif isinstance(cur, list) and isinstance(k, int) and len(cur) > k:
            cur = cur[k]
        else:
            return default
    return cur if cur is not None else default


def _ts(v) -> datetime | None:
    """ISO8601 ya da unix saniye/milisaniye kabul eder."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(float(v) / (1000 if v > 1e11 else 1), tz=timezone.utc)
        except (ValueError, OSError):
            return None
    s = str(v).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _token_address_from_id(raw: str | None) -> str | None:
    """'solana_EPjFW...' -> 'EPjFW...'  ('eth_0xabc' -> '0xabc')"""
    if not raw:
        return None
    return raw.split("_", 1)[1] if "_" in raw else raw


# --------------------------------------------------------------------------- #
#  Tipler
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Pool:
    chain: str
    pair_address: str
    token_address: str | None
    symbol: str | None
    name: str | None
    dex_id: str | None
    price_usd: float | None
    mc_usd: float | None
    liquidity_usd: float | None
    volume_h1: float | None
    volume_h24: float | None
    created_at: datetime | None
    buys_h1: int | None = None
    sells_h1: int | None = None


@dataclass(slots=True)
class SwapTrade:
    wallet: str
    side: str                    # buy | sell
    at: datetime
    usd: float | None
    price_usd: float | None
    tx_hash: str


# --------------------------------------------------------------------------- #
#  Istemci
# --------------------------------------------------------------------------- #
class GeckoTerminal:
    def __init__(self, http: HttpClient, base: str = BASE) -> None:
        self.http = http
        self.base = base.rstrip("/")

    # ------------------------------------------------------------- havuzlar
    def _parse_pool(self, item: dict, chain: str) -> Pool | None:
        if not isinstance(item, dict):
            return None
        a = item.get("attributes") or {}
        pair = a.get("address") or _token_address_from_id(item.get("id"))
        if not pair:
            return None

        base_id = _dig(item, "relationships", "base_token", "data", "id")
        token_addr = _token_address_from_id(base_id)

        # "PEPE / SOL" seklinde gelir; sol taraf base token sembolu.
        raw_name = a.get("name") or ""
        symbol = raw_name.split("/")[0].strip() or None

        return Pool(
            chain=chain,
            pair_address=str(pair),
            token_address=token_addr,
            symbol=symbol,
            name=raw_name or None,
            dex_id=_dig(item, "relationships", "dex", "data", "id"),
            price_usd=_f(a.get("base_token_price_usd")),
            mc_usd=_f(a.get("market_cap_usd")) or _f(a.get("fdv_usd")),
            liquidity_usd=_f(a.get("reserve_in_usd")),
            volume_h1=_f(_dig(a, "volume_usd", "h1")),
            volume_h24=_f(_dig(a, "volume_usd", "h24")),
            created_at=_ts(a.get("pool_created_at")),
            buys_h1=_dig(a, "transactions", "h1", "buys"),
            sells_h1=_dig(a, "transactions", "h1", "sells"),
        )

    async def _pools(self, chain: str, endpoint: str, pages: int) -> list[Pool]:
        net = GT_NETWORK.get(chain)
        if not net:
            return []
        out: list[Pool] = []
        for page in range(1, max(1, pages) + 1):
            data = await self.http.get(
                f"{self.base}/networks/{net}/{endpoint}",
                bucket=BUCKET,
                params={"page": page},
            )
            items = (data or {}).get("data") or []
            if not items:
                break
            for it in items:
                p = self._parse_pool(it, chain)
                if p:
                    out.append(p)
        return out

    async def new_pools(self, chain: str, pages: int = 3) -> list[Pool]:
        return await self._pools(chain, "new_pools", pages)

    async def trending_pools(self, chain: str, pages: int = 2) -> list[Pool]:
        return await self._pools(chain, "trending_pools", pages)

    async def pool(self, chain: str, pair_address: str) -> Pool | None:
        net = GT_NETWORK.get(chain)
        if not net:
            return None
        data = await self.http.get(
            f"{self.base}/networks/{net}/pools/{pair_address}", bucket=BUCKET
        )
        item = (data or {}).get("data")
        return self._parse_pool(item, chain) if isinstance(item, dict) else None

    async def top_pool_for_token(self, chain: str, token_address: str) -> Pool | None:
        """Bir tokenin en likit havuzu."""
        net = GT_NETWORK.get(chain)
        if not net:
            return None
        data = await self.http.get(
            f"{self.base}/networks/{net}/tokens/{token_address}/pools", bucket=BUCKET
        )
        items = (data or {}).get("data") or []
        pools = [p for p in (self._parse_pool(i, chain) for i in items) if p]
        if not pools:
            return None
        return max(pools, key=lambda p: p.liquidity_usd or 0.0)

    # -------------------------------------------------------------- islemler
    async def trades(
        self, chain: str, pair_address: str, min_usd: float = 0.0
    ) -> list[SwapTrade]:
        """Son ~300 takas. `tx_from_address` gercek imzalayan cuzdandir."""
        net = GT_NETWORK.get(chain)
        if not net:
            return []
        params: dict = {}
        if min_usd > 0:
            params["trade_volume_in_usd_greater_than"] = int(min_usd)
        data = await self.http.get(
            f"{self.base}/networks/{net}/pools/{pair_address}/trades",
            bucket=BUCKET,
            params=params or None,
        )
        out: list[SwapTrade] = []
        for it in (data or {}).get("data") or []:
            a = (it or {}).get("attributes") or {}
            wallet = a.get("tx_from_address")
            at = _ts(a.get("block_timestamp"))
            tx = a.get("tx_hash") or (it or {}).get("id")
            if not wallet or at is None or not tx:
                continue
            kind = str(a.get("kind") or "").lower()
            if kind not in ("buy", "sell"):
                continue
            # Alimda hedef token "to", satista "from" tarafindadir.
            price = _f(a.get("price_to_in_usd")) if kind == "buy" else _f(a.get("price_from_in_usd"))
            out.append(
                SwapTrade(
                    wallet=str(wallet),
                    side=kind,
                    at=at,
                    usd=_f(a.get("volume_in_usd")),
                    price_usd=price,
                    tx_hash=str(tx),
                )
            )
        return out

    # ------------------------------------------------------------------ tani
    async def diagnose(self, chain: str) -> dict:
        net = GT_NETWORK.get(chain, chain)
        data = await self.http.get(
            f"{self.base}/networks/{net}/new_pools", bucket=BUCKET, params={"page": 1}
        )
        items = (data or {}).get("data") or []
        return {
            "kaynak": "geckoterminal",
            "ag": net,
            "ok": bool(items),
            "havuz": len(items),
            "hata": self.http.last_error,
            "ornek": (items[0] if items else None),
        }
