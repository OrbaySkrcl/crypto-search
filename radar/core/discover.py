"""ADIM 1 — KESIF: hangi tokenler izlenmeye deger?

Evreni bilerek kucuk tutuyoruz. Solana'da gunde binlerce havuz doguyor
ama bunlarin ezici cogunlugu girilemez: $2.000 likiditede gorunen 50x
kagit uzerindedir. Likidite tabani koyup evreni birkac yuze indirmek
hem bedava kotayi korur hem de olcumu durustlestirir.

Iki kaynak:
  * new_pools      — akisin kendisi
  * trending_pools — kacirilanlari toplar
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, select

from ..config import settings
from ..db import Token, session_scope, utcnow
from ..http import HttpClient
from ..sources.geckoterminal import GeckoTerminal, Pool
from . import repo

log = logging.getLogger(__name__)


def _worth_tracking(p: Pool) -> tuple[bool, str]:
    if not p.token_address:
        return False, "token adresi yok"
    liq = p.liquidity_usd or 0.0
    if liq < settings.min_pool_liquidity_usd:
        return False, f"likidite ${liq:,.0f} < taban"
    if p.created_at:
        age_min = (utcnow() - p.created_at).total_seconds() / 60
        if age_min > settings.max_pool_age_minutes:
            return False, "havuz cok eski"
    return True, ""


async def discover_once(http: HttpClient) -> dict:
    """Bir kesif turu. Yeni ve trend havuzlari tokene cevirip kaydeder."""
    gt = GeckoTerminal(http)
    stats = {"gorulen": 0, "yeni": 0, "guncellenen": 0, "elenen": 0, "zincir": {}}

    for chain in settings.chains:
        pools: list[Pool] = []
        pools += await gt.new_pools(chain, settings.discover_new_pages)
        pools += await gt.trending_pools(chain, settings.discover_trending_pages)

        seen: set[str] = set()
        c_new = c_upd = c_skip = 0
        with session_scope() as s:
            for p in pools:
                stats["gorulen"] += 1
                key = p.token_address or p.pair_address
                if key in seen:
                    continue
                seen.add(key)

                ok, _reason = _worth_tracking(p)
                if not ok:
                    c_skip += 1
                    continue

                tok, created = repo.upsert_token(
                    s,
                    chain,
                    p.token_address,
                    symbol=p.symbol,
                    name=p.name,
                    pair_address=p.pair_address,
                    dex_id=p.dex_id,
                    price_usd=p.price_usd,
                    mc_usd=p.mc_usd,
                    liquidity_usd=p.liquidity_usd,
                    volume_h1=p.volume_h1,
                    volume_h24=p.volume_h24,
                    pool_created_at=p.created_at,
                )
                if created:
                    c_new += 1
                else:
                    # Kapasite yuzunden dusurulmus bir token yeniden trend
                    # oluyorsa geri al: dusurulmesi kalici bir karar degildi.
                    if not tok.active:
                        tok.active = True
                    c_upd += 1

        stats["yeni"] += c_new
        stats["guncellenen"] += c_upd
        stats["elenen"] += c_skip
        stats["zincir"][chain] = {"yeni": c_new, "guncel": c_upd, "elenen": c_skip}

    _enforce_capacity()
    return stats


def _enforce_capacity() -> None:
    """Aktif token sayisini tavanda tutar.

    Kota koruma kurali: kullanicinin takip listesindeki ve son 48 saatte
    alarm uretmis tokenler ASLA dusurulmez; geri kalanlar likiditeye gore
    siralanir, tavanin altindakiler pasife alinir.
    """
    from ..db import Alert, Watch

    cap = max(50, settings.max_active_tokens)
    with session_scope() as s:
        n_active = s.scalar(
            select(func.count(Token.id)).where(Token.active.is_(True))
        ) or 0
        if n_active <= cap:
            return

        protected = set(s.scalars(select(Watch.token_id)))
        recent = utcnow() - timedelta(hours=48)
        protected |= set(
            s.scalars(select(Alert.token_id).where(Alert.created_at >= recent))
        )

        rows = list(
            s.scalars(
                select(Token)
                .where(Token.active.is_(True))
                .order_by(Token.last_liquidity_usd.desc().nulls_last())
            )
        )
        kept = 0
        dropped = 0
        for tok in rows:
            if tok.id in protected:
                kept += 1
                continue
            if kept < cap:
                kept += 1
                continue
            tok.active = False
            dropped += 1
        if dropped:
            log.info("kapasite: %s token pasife alindi (tavan %s)", dropped, cap)
