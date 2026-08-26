"""Kosan tokenlarin erken alicilarindan cuzdan profili cikarir.

Mantik Twitter tarafiyla ayni, yalnizca "cagri" tanimi degisiyor:
    tweet katmani : @x, T aninda CA'yi paylasti
    zincir katmani: cuzdan W, T aninda tokeni ALDI

Ayni Call tablosu, ayni fiyat oracle'i, ayni skorlama. Fark su ki alim
tweet'ten once gerceklesir -- insan once alir, sonra tweetler.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db.models import Call, CallSource, Token, TokenStatus, Wallet, utcnow
from ..db.session import session_scope
from ..http import HttpClient
from ..oracle.dexscreener import DexScreenerClient
from ..pipeline import repo
from ..scoring import metrics as M
from .trades import TradeSource
from .types import BuyEvent

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Cuzdan kaydi
# --------------------------------------------------------------------------- #
def upsert_wallet(session: Session, chain: str, address: str) -> Wallet:
    w = session.scalar(
        select(Wallet).where(Wallet.chain == chain, Wallet.address == address)
    )
    if w is None:
        w = Wallet(chain=chain, address=address)
        session.add(w)
        session.flush()
    else:
        w.last_seen_at = utcnow()
    return w


def create_wallet_call(
    session: Session, wallet: Wallet, token: Token, ev: BuyEvent
) -> tuple[Call, bool]:
    existing = session.scalar(
        select(Call).where(Call.wallet_id == wallet.id, Call.tx_signature == ev.tx)
    ) if ev.tx else session.scalar(
        select(Call).where(
            Call.wallet_id == wallet.id,
            Call.token_id == token.id,
            Call.called_at == ev.at,
        )
    )
    if existing:
        return (existing, False)

    call = Call(
        source=CallSource.WALLET,
        wallet_id=wallet.id,
        token_id=token.id,
        chain=ev.chain,
        called_at=ev.at,
        buy_usd=ev.usd,
        tx_signature=ev.tx,
    )
    if ev.price_usd and token.supply_estimate:
        call.entry_price_usd = ev.price_usd
        call.entry_mc_usd = ev.price_usd * token.supply_estimate
        call.entry_source = "onchain_trade"
        # Zincirdeki islem fiyati kesindir -- OHLCV tahmininden daha iyi
        call.entry_confidence = 1.0
        call.mc_earliness = M.mc_earliness(
            call.entry_mc_usd, settings.mc_earliness_low_usd, settings.mc_earliness_high_usd
        )
    if token.pair_created_at:
        call.token_age_at_call_sec = int(
            (ev.at - repo._aware(token.pair_created_at)).total_seconds()
        )
    session.add(call)
    session.flush()
    return (call, True)


# --------------------------------------------------------------------------- #
#  Bot / sniper elemesi
# --------------------------------------------------------------------------- #
def classify_wallet(session: Session, wallet: Wallet) -> None:
    """Ham kazanma oraninda botlar uste cikar; ayirmak sart.

    Bir bot her yeni havuza ilk saniyelerde girer ve yuzlerce tokene dokunur.
    Insan alfasi ise seciciydir ve dakikalar/saatler icinde girer.
    """
    calls = list(session.scalars(select(Call).where(Call.wallet_id == wallet.id)))
    wallet.distinct_tokens = len({c.token_id for c in calls})

    delays = [c.token_age_at_call_sec for c in calls if c.token_age_at_call_sec is not None]
    if delays:
        delays.sort()
        wallet.median_entry_delay_sec = delays[len(delays) // 2]

    reasons = []
    if wallet.median_entry_delay_sec is not None and wallet.median_entry_delay_sec < 20:
        reasons.append(f"medyan giris {wallet.median_entry_delay_sec}sn (sniper bot)")
    if wallet.distinct_tokens >= settings.wallet_bot_token_threshold:
        reasons.append(f"{wallet.distinct_tokens} farkli token (tarama botu)")

    wallet.is_bot = bool(reasons)
    wallet.bot_reason = "; ".join(reasons)[:160] if reasons else None


# --------------------------------------------------------------------------- #
#  Token profilleme
# --------------------------------------------------------------------------- #
async def profile_token(
    chain: str,
    address: str,
    max_entry_mc: float | None = None,
    max_buyers: int = 60,
) -> dict:
    """Bir tokenin erken alicilarini cuzdan cagrilarina cevirir.

    `max_entry_mc`: yalnizca bu piyasa degerinin ALTINDA alanlar sayilir --
    coin uctuktan sonra girenler erken alici degildir.
    """
    stats = {
        "token": address, "chain": chain,
        "alim": 0, "cuzdan": 0, "yeni_cagri": 0, "elenen": 0, "not": None,
    }
    cap = max_entry_mc if max_entry_mc is not None else settings.wallet_max_entry_mc_usd

    async with HttpClient() as http:
        info = await DexScreenerClient(http).resolve_pair_or_token(chain, address)
        if info is None:
            stats["not"] = "token DEX'te bulunamadi"
            return stats

        src = TradeSource(http)
        events = await src.early_buyers(chain, info.address, max_events=max_buyers * 4)
        stats["alim"] = len(events)
        if not events:
            stats["not"] = src.last_detail or "erken alim bulunamadi"
            return stats

    with session_scope() as s:
        token = repo.upsert_token(s, chain, info.address, info)
        supply = token.supply_estimate
        wallets: set[str] = set()

        for ev in events:
            if len(wallets) >= max_buyers:
                break
            # Coin uctuktan sonra girenler "erken alici" degildir
            if supply and ev.price_usd and cap:
                if ev.price_usd * supply > cap:
                    stats["elenen"] += 1
                    continue
            w = upsert_wallet(s, chain, ev.wallet)
            wallets.add(ev.wallet)
            _call, created = create_wallet_call(s, w, token, ev)
            if created:
                stats["yeni_cagri"] += 1

        stats["cuzdan"] = len(wallets)
        for addr in wallets:
            w = s.scalar(select(Wallet).where(Wallet.chain == chain, Wallet.address == addr))
            if w:
                classify_wallet(s, w)
        repo.refresh_caller_ranks(s, token.id)

    log.info(
        "token profillendi: %s -> %d alim, %d cuzdan, %d yeni cagri",
        address[:10], stats["alim"], stats["cuzdan"], stats["yeni_cagri"],
    )
    return stats


# --------------------------------------------------------------------------- #
#  Kosan token kesfi
# --------------------------------------------------------------------------- #
def find_runner_tokens(session: Session, min_multiple: float = 5.0, limit: int = 25) -> list[Token]:
    """Veritabanindaki tokenlardan gercekten kosmus olanlar.

    Bunlarin erken alicilari, cuzdan avinin av sahasi.
    """
    rows = list(
        session.scalars(
            select(Token)
            .where(
                Token.status != TokenStatus.INVALID,
                Token.ath_mc_usd.isnot(None),
                Token.supply_estimate.isnot(None),
            )
            .order_by(Token.ath_mc_usd.desc())
            .limit(limit * 6)
        )
    )
    out = []
    for t in rows:
        # Taban: bu token icin gordugumuz en dusuk giris
        floor = session.scalar(
            select(func.min(Call.entry_mc_usd)).where(Call.token_id == t.id)
        ) or t.launch_mc_usd
        if not floor or floor <= 0:
            continue
        if (t.ath_mc_usd or 0) / floor >= min_multiple:
            out.append(t)
        if len(out) >= limit:
            break
    return out


def recently_profiled(session: Session, token_id: int, within_hours: int = 24) -> bool:
    n = session.scalar(
        select(func.count(Call.id)).where(
            Call.token_id == token_id,
            Call.source == CallSource.WALLET,
            Call.created_at >= utcnow() - timedelta(hours=within_hours),
        )
    )
    return bool(n)
