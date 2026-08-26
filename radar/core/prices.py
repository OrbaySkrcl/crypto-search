"""ADIM 2 — FIYAT: kendi gecmisimizi biriktiriyoruz.

Neden onemli: ucretli tarihsel fiyat API'si (Birdeye, Nansen) aylik
$100+. Ama biz zaten her 5 dakikada bir toplu sorgu atiyoruz; bunu
kaydedersek 30 gunluk kendi gecmisimiz olur ve karne icin ucuncu tarafa
hic ihtiyac kalmaz.

DexScreener toplu uctan 30 token/istek gelir: 300 token = 10 istek.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from ..config import settings
from ..db import Token, TokenStatus, session_scope, utcnow
from ..http import HttpClient
from ..sources.dexscreener import DexScreener
from . import repo

log = logging.getLogger(__name__)

# Likidite bu orana duserse token "rug" sayilir.
RUG_DROP = 0.85
RUG_FLOOR_USD = 1_500.0


async def refresh_prices(http: HttpClient) -> dict:
    """Aktif tokenlerin fiyatini tazeler ve ornek kaydeder."""
    ds = DexScreener(http)
    stats = {"sorgulanan": 0, "guncellenen": 0, "cevapsiz": 0, "rug": 0}

    by_chain: dict[str, list[str]] = defaultdict(list)
    with session_scope() as s:
        for tok in repo.active_tokens(s, limit=settings.max_active_tokens):
            by_chain[tok.chain].append(tok.address)

    for chain, addrs in by_chain.items():
        stats["sorgulanan"] += len(addrs)
        snaps = await ds.tokens(addrs, chain=chain)
        with session_scope() as s:
            for addr in addrs:
                tok = repo.get_token(s, chain, addr)
                if tok is None:
                    continue
                snap = snaps.get(addr)
                if snap is None:
                    # Cevap yok: dusurmuyoruz ama tekrar tekrar sormamak icin
                    # kontrol zamanini ilerletiyoruz.
                    tok.last_checked_at = utcnow()
                    stats["cevapsiz"] += 1
                    continue

                repo.upsert_token(
                    s,
                    chain,
                    addr,
                    symbol=snap.symbol,
                    name=snap.name,
                    pair_address=snap.pair_address,
                    dex_id=snap.dex_id,
                    price_usd=snap.price_usd,
                    mc_usd=snap.mc_usd,
                    liquidity_usd=snap.liquidity_usd,
                    volume_h1=snap.volume_h1,
                    volume_h24=snap.volume_h24,
                    pool_created_at=snap.pair_created_at,
                )
                repo.record_snapshot(s, tok)
                _update_peak(s, tok)
                if _detect_rug(tok):
                    tok.status = TokenStatus.RUG
                    tok.active = False
                    stats["rug"] += 1
                stats["guncellenen"] += 1

    return stats


def _update_peak(session, token: Token) -> None:
    """Korunmus tepeyi guncel tutar (her ornekte yeniden hesaplamak pahali)."""
    mc = token.last_mc_usd
    if not mc:
        return
    if token.peak_mc_usd is None or mc > token.peak_mc_usd:
        # Ham tepe. Karne ve cuzdan skorlamasi sustained_peak() ile
        # ORNEKLERDEN yeniden hesaplar; bu alan yalnizca hizli gosterim icin.
        token.peak_mc_usd = mc
        token.peak_at = utcnow()


def _detect_rug(token: Token) -> bool:
    first = token.first_liquidity_usd or 0.0
    last = token.last_liquidity_usd
    if last is None:
        return False
    if last < RUG_FLOOR_USD and first >= RUG_FLOOR_USD * 2:
        return True
    return bool(first > 0 and last < first * (1 - RUG_DROP))
