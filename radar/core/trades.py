"""ADIM 3 — ISLEM ORNEKLEME: botun kalbi.

Tek bir bedava uc (GeckoTerminal /trades) hem cuzdan sicilini kurar hem
de canli konfluansi yakalar. Ayri bir "cuzdan izleme" altyapisina gerek
yok: akilli cuzdan zaten izledigimiz havuzlarda alim yapiyor.

Ornekleme stratejisi (kota bilincli):
  1. Kullanicinin takip listesindeki tokenler — her turda
  2. Son 24 saatte alarm ureten tokenler
  3. Geri kalanlar arasinda hacim/likidite orani en yuksek olanlar
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select

from ..config import settings
from ..db import Alert, Token, TokenStatus, Watch, session_scope, utcnow
from ..http import HttpClient
from ..sources.geckoterminal import GeckoTerminal
from . import repo

log = logging.getLogger(__name__)


def _pick_targets(session, limit: int) -> list[Token]:
    """Ornekleme onceligi."""
    picked: list[Token] = []
    seen: set[int] = set()

    def add(tokens) -> None:
        for t in tokens:
            if t.id in seen or len(picked) >= limit:
                continue
            if not t.pair_address:
                continue
            seen.add(t.id)
            picked.append(t)

    watch_ids = set(session.scalars(select(Watch.token_id)))
    if watch_ids:
        add(session.scalars(select(Token).where(Token.id.in_(watch_ids), Token.active.is_(True))))

    recent = utcnow() - timedelta(hours=24)
    alert_ids = set(session.scalars(select(Alert.token_id).where(Alert.created_at >= recent)))
    if alert_ids:
        add(session.scalars(select(Token).where(Token.id.in_(alert_ids), Token.active.is_(True))))

    # Geri kalan: en uzun suredir ornek alinmamis olanlar once.
    #
    # Bilerek hacme gore siralamiyoruz. Yakalamak istedigimiz sey tam olarak
    # "sessiz tokende erken birikim"; hacme gore siralamak o tokenleri en sona
    # atardi ve bot yalnizca zaten gorulen seyleri gorurdu.
    add(
        session.scalars(
            select(Token)
            .where(
                Token.active.is_(True),
                Token.status == TokenStatus.LIVE,
                Token.pair_address.is_not(None),
            )
            .order_by(Token.trades_synced_at.asc().nulls_first())
            .limit(limit * 3)
        )
    )
    return picked


async def sample_trades(http: HttpClient) -> dict:
    """Secilen tokenlerin son takaslarini ceker ve kaydeder."""
    gt = GeckoTerminal(http)
    stats = {"token": 0, "islem": 0, "yeni_islem": 0}

    with session_scope() as s:
        targets = [
            (t.id, t.chain, t.address, t.pair_address)
            for t in _pick_targets(s, settings.trades_sample_per_cycle)
        ]

    for token_id, chain, _addr, pair in targets:
        trades = await gt.trades(chain, pair, min_usd=settings.min_trade_usd)
        stats["token"] += 1
        if not trades:
            with session_scope() as s:
                tok = s.get(Token, token_id)
                if tok:
                    tok.trades_synced_at = utcnow()
            continue

        with session_scope() as s:
            tok = s.get(Token, token_id)
            if tok is None:
                continue
            for tr in trades:
                stats["islem"] += 1
                if tr.usd is not None and tr.usd < settings.min_trade_usd:
                    continue
                wallet = repo.upsert_wallet(s, chain, tr.wallet)
                _rec, created = repo.record_trade(
                    s,
                    tok,
                    wallet,
                    side=tr.side,
                    at=tr.at,
                    usd=tr.usd,
                    price_usd=tr.price_usd,
                    tx_hash=tr.tx_hash,
                )
                if created:
                    stats["yeni_islem"] += 1
                    if tr.side == "buy":
                        wallet.n_buys += 1
                    else:
                        wallet.n_sells += 1
            tok.trades_synced_at = utcnow()

    return stats
