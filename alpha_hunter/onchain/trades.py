"""Zincir uzerindeki alimlari ceken katman (Birdeye).

Twitter'la fark: bu veri bedava/ucuz, eksiksiz ve ERKEN. Bir tokenin ilk
alicilari, o token hakkinda tweet atilmadan cok once zincirde duruyor.

Apify dersinden ogrenilen disiplin burada da uygulaniyor:
  * birden fazla uc denenir, calisan hatirlanmaz ama raporlanir
  * cozumleyici alan adlarina esnek
  * diagnose() ham yaniti gosterir -- tahmin yurutmeye gerek kalmaz
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..config import settings
from ..http import HttpClient
from .types import BuyEvent

log = logging.getLogger(__name__)

CHAIN_HEADER = {
    "solana": "solana", "ethereum": "ethereum", "base": "base",
    "bsc": "bsc", "arbitrum": "arbitrum", "polygon": "polygon",
}

# Birdeye surumleri arasinda uc adi degisiyor; sirayla denenir.
_TX_ENDPOINTS = [
    ("v3", "/defi/v3/token/txs", {"sort_type": "asc", "tx_type": "swap"}),
    ("v1", "/defi/txs/token", {"sort_type": "asc", "tx_type": "swap"}),
]


def _f(v) -> float | None:
    try:
        if v in (None, "", "N/A"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _dig(d, *path):
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _pick(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def normalise_trade(row: dict, token_address: str, chain: str) -> BuyEvent | None:
    """Birdeye islem kaydini BuyEvent'e cevirir.

    Yalnizca ALIMLAR dondurulur: hedef token cuzdana GIRIYOR olmali.
    Satislar ve baska tokenlarin islemleri elenir.
    """
    if not isinstance(row, dict):
        return None

    wallet = _pick(row, "owner", "wallet", "trader", "signer", "from_address")
    if not wallet:
        wallet = _dig(row, "from", "owner") or _dig(row, "owner", "address")
    if not wallet:
        return None

    ts_raw = _pick(row, "blockUnixTime", "block_unix_time", "unixTime", "timestamp", "time")
    if ts_raw is None:
        return None
    try:
        ts = float(ts_raw)
        at = datetime.fromtimestamp(ts / (1000 if ts > 1e11 else 1), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None

    # Alim mi satis mi? Once acik "side" alanina bak, yoksa hangi tarafta
    # hedef token oldugundan cikar.
    side = str(_pick(row, "side", "txType", "type", default="") or "").lower()
    to_addr = str(_dig(row, "to", "address") or "").lower()
    from_addr = str(_dig(row, "from", "address") or "").lower()
    target = token_address.lower()

    if side in ("sell", "sells"):
        return None
    if side not in ("buy", "buys"):
        if to_addr and to_addr != target:
            return None                     # hedef token cikiyor -> satis
        if not to_addr and from_addr == target:
            return None

    usd = _f(_pick(row, "volumeUsd", "volume_usd", "valueUsd", "value_usd", "usdValue"))
    price = _f(_pick(row, "priceUsd", "price_usd", "price"))
    if price is None:
        price = _f(_dig(row, "to", "price")) or _f(_dig(row, "from", "price"))

    return BuyEvent(
        wallet=str(wallet),
        token_address=token_address,
        chain=chain,
        at=at,
        usd=usd,
        price_usd=price,
        tx=_pick(row, "txHash", "tx_hash", "signature", "txSignature"),
        source=_pick(row, "source", "dex", "poolId"),
    )


class TradeSource:
    name = "birdeye_trades"

    def __init__(self, http: HttpClient, api_key: str | None = None) -> None:
        self.http = http
        self.key = api_key or settings.birdeye_api_key
        self.base = settings.birdeye_base.rstrip("/")
        self.last_detail: str | None = None
        self.last_endpoint: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def _headers(self, chain: str) -> dict[str, str]:
        return {
            "X-API-KEY": self.key or "",
            "x-chain": CHAIN_HEADER.get(chain, "solana"),
            "Accept": "application/json",
        }

    async def _fetch_page(
        self, chain: str, address: str, offset: int, limit: int
    ) -> tuple[list[dict], str | None]:
        for name, path, extra in _TX_ENDPOINTS:
            params = {"address": address, "offset": str(offset), "limit": str(limit), **extra}
            data = await self.http.get(
                f"{self.base}{path}", bucket="birdeye",
                params=params, headers=self._headers(chain), max_retries=1,
            )
            items = _dig(data, "data", "items") or _dig(data, "data", "txs")
            if isinstance(items, list):
                self.last_endpoint = name
                return (items, None)
            if isinstance(data, dict) and data.get("message"):
                return ([], f"{name}: {str(data['message'])[:140]}")
        return ([], "hicbir islem ucu yanit vermedi")

    async def early_buyers(
        self,
        chain: str,
        token_address: str,
        max_events: int = 200,
        max_pages: int = 4,
    ) -> list[BuyEvent]:
        """Tokenin EN ERKEN alimlari, zaman sirasiyla.

        Bir tokenin ilk alicilari, o tokenin kaderini bilenlerdir -- ya da
        sadece hizli botlardir. Ayirmak profiler'in isi.
        """
        self.last_detail = None
        if not self.enabled:
            self.last_detail = "BIRDEYE_API_KEY tanimli degil"
            return []

        out: list[BuyEvent] = []
        seen: set[tuple[str, str]] = set()
        page_size = 50
        for page in range(max_pages):
            rows, err = await self._fetch_page(
                chain, token_address, page * page_size, page_size
            )
            if err:
                self.last_detail = err
                break
            if not rows:
                if page == 0:
                    self.last_detail = "islem kaydi bulunamadi (token cok yeni olabilir)"
                break
            for r in rows:
                ev = normalise_trade(r, token_address, chain)
                if ev is None:
                    continue
                k = ev.key()
                if k in seen:
                    continue
                seen.add(k)
                out.append(ev)
            if len(out) >= max_events:
                break

        out.sort(key=lambda e: e.at)
        return out[:max_events]

    async def diagnose(self, chain: str, token_address: str) -> dict:
        """Tek gercek cagri, ham sonuc. Apify'da ogrendigimiz ders."""
        out: dict = {
            "anahtar_var": bool(self.key),
            "zincir": chain,
            "token": token_address,
        }
        if not self.enabled:
            out["sonuc"] = "BIRDEYE_API_KEY tanimli degil"
            return out

        rows, err = await self._fetch_page(chain, token_address, 0, 10)
        out["calisan_uc"] = self.last_endpoint
        out["hata"] = err
        out["ham_kayit"] = len(rows)
        if rows:
            first = rows[0] if isinstance(rows[0], dict) else {}
            out["alan_adlari"] = sorted(first.keys())[:25]
            olaylar = [normalise_trade(r, token_address, chain) for r in rows]
            olaylar = [e for e in olaylar if e]
            out["cozulen_alim"] = len(olaylar)
            if olaylar:
                e = olaylar[0]
                out["ornek"] = {
                    "cuzdan": e.wallet[:12] + "...", "zaman": e.at.isoformat(),
                    "usd": e.usd, "fiyat": e.price_usd,
                }
            out["sonuc"] = "calisiyor" if olaylar else "kayit geliyor ama alim cozulemedi"
        else:
            out["sonuc"] = err or "kayit gelmedi"
        return out
