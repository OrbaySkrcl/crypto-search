"""Veritabani yardimcilari — upsert ve secim sorgulari tek yerde."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import PriceSnapshot, Token, TokenStatus, Trade, Wallet, utcnow

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Token
# --------------------------------------------------------------------------- #
def get_token(session: Session, chain: str, address: str) -> Token | None:
    return session.scalar(
        select(Token).where(Token.chain == chain, Token.address == address)
    )


def upsert_token(
    session: Session,
    chain: str,
    address: str,
    *,
    symbol: str | None = None,
    name: str | None = None,
    pair_address: str | None = None,
    dex_id: str | None = None,
    price_usd: float | None = None,
    mc_usd: float | None = None,
    liquidity_usd: float | None = None,
    volume_h1: float | None = None,
    volume_h24: float | None = None,
    pool_created_at: datetime | None = None,
) -> tuple[Token, bool]:
    """(token, yeni_mi) doner."""
    tok = get_token(session, chain, address)
    created = False
    if tok is None:
        tok = Token(
            chain=chain,
            address=address,
            first_price_usd=price_usd,
            first_mc_usd=mc_usd,
            first_liquidity_usd=liquidity_usd,
        )
        session.add(tok)
        created = True

    if symbol:
        tok.symbol = symbol
    if name:
        tok.name = name
    if pair_address:
        tok.pair_address = pair_address
    if dex_id:
        tok.dex_id = dex_id
    if pool_created_at and not tok.pool_created_at:
        tok.pool_created_at = pool_created_at

    if price_usd is not None:
        tok.last_price_usd = price_usd
    if mc_usd is not None:
        tok.last_mc_usd = mc_usd
        if tok.first_mc_usd is None:
            tok.first_mc_usd = mc_usd
    if liquidity_usd is not None:
        tok.last_liquidity_usd = liquidity_usd
        if tok.first_liquidity_usd is None:
            tok.first_liquidity_usd = liquidity_usd
    if volume_h1 is not None:
        tok.last_volume_h1 = volume_h1
    if volume_h24 is not None:
        tok.last_volume_h24 = volume_h24
    tok.last_checked_at = utcnow()
    session.flush()
    return tok, created


def record_snapshot(session: Session, token: Token) -> PriceSnapshot:
    snap = PriceSnapshot(
        token_id=token.id,
        at=utcnow(),
        price_usd=token.last_price_usd,
        mc_usd=token.last_mc_usd,
        liquidity_usd=token.last_liquidity_usd,
        volume_h1=token.last_volume_h1,
    )
    session.add(snap)
    return snap


def set_safety(session: Session, token: Token, score: float, flags: list[str]) -> None:
    token.safety_score = score
    token.safety_flags = json.dumps(flags[:12], ensure_ascii=False)
    token.safety_checked_at = utcnow()
    session.flush()


def active_tokens(session: Session, chain: str | None = None, limit: int | None = None) -> list[Token]:
    q = select(Token).where(Token.active.is_(True), Token.status == TokenStatus.LIVE)
    if chain:
        q = q.where(Token.chain == chain)
    # En eski kontrol edilen once: hepsi sirayla taranir, kimse ac kalmaz.
    q = q.order_by(Token.last_checked_at.asc().nulls_first())
    if limit:
        q = q.limit(limit)
    return list(session.scalars(q))


def watched_token_ids(session: Session) -> set[int]:
    from ..db import Watch

    return set(session.scalars(select(Watch.token_id)))


# --------------------------------------------------------------------------- #
#  Cuzdan / islem
# --------------------------------------------------------------------------- #
def upsert_wallet(session: Session, chain: str, address: str) -> Wallet:
    w = session.scalar(select(Wallet).where(Wallet.chain == chain, Wallet.address == address))
    if w is None:
        w = Wallet(chain=chain, address=address)
        session.add(w)
        session.flush()
    else:
        w.last_seen_at = utcnow()
    return w


def record_trade(
    session: Session,
    token: Token,
    wallet: Wallet,
    *,
    side: str,
    at: datetime,
    usd: float | None,
    price_usd: float | None,
    tx_hash: str,
) -> tuple[Trade, bool]:
    existing = session.scalar(
        select(Trade).where(
            Trade.token_id == token.id,
            Trade.wallet_id == wallet.id,
            Trade.tx_hash == tx_hash,
        )
    )
    if existing is not None:
        return existing, False

    mc = None
    if price_usd and token.last_price_usd and token.last_mc_usd:
        # Arz sabit varsayilir: MC = fiyat * arz, arz = son_MC / son_fiyat
        supply = token.last_mc_usd / token.last_price_usd
        mc = price_usd * supply

    tr = Trade(
        token_id=token.id,
        wallet_id=wallet.id,
        chain=token.chain,
        side=side,
        at=at,
        usd=usd,
        price_usd=price_usd,
        mc_usd=mc,
        tx_hash=tx_hash,
    )
    session.add(tr)
    session.flush()
    return tr, True


def smart_wallet_ids(session: Session) -> set[int]:
    return set(
        session.scalars(
            select(Wallet.id).where(Wallet.smart.is_(True), Wallet.blocked.is_(False))
        )
    )


# --------------------------------------------------------------------------- #
#  Fiyat gecmisi uzerinden hesaplar
# --------------------------------------------------------------------------- #
def sustained_peak(
    session: Session, token_id: int, since: datetime, until: datetime | None = None
) -> tuple[float | None, datetime | None]:
    """`since` sonrasindaki EN AZ sustained_minutes korunmus tepe piyasa degeri.

    ATH kagit uzerindedir: 30 saniye gorunen fiyattan cikamazsin. Bir
    seviyenin gecerli sayilmasi icin ardisik iki ornekte de korunmasi gerekir.
    """
    q = (
        select(PriceSnapshot.at, PriceSnapshot.mc_usd)
        .where(PriceSnapshot.token_id == token_id, PriceSnapshot.at >= since)
        .order_by(PriceSnapshot.at.asc())
    )
    if until is not None:
        q = q.where(PriceSnapshot.at <= until)
    rows = [(at, mc) for at, mc in session.execute(q) if mc]
    if not rows:
        return (None, None)
    if len(rows) == 1:
        return (rows[0][1], rows[0][0])

    hold = timedelta(minutes=settings.sustained_minutes)
    best: float | None = None
    best_at: datetime | None = None
    for i, (at_i, mc_i) in enumerate(rows):
        # mc_i seviyesi, at_i'den itibaren `hold` boyunca korundu mu?
        deadline = at_i + hold
        kept = False
        for at_j, mc_j in rows[i + 1 :]:
            if mc_j < mc_i * 0.98:          # %2 tolerans
                break
            if at_j >= deadline:
                kept = True
                break
        if kept and (best is None or mc_i > best):
            best, best_at = mc_i, at_i
    if best is None:
        # Hicbir seviye korunmadi -> en dusuk tepe olarak son ornegi al
        return (rows[-1][1], rows[-1][0])
    return (best, best_at)


def price_at(session: Session, token_id: int, when: datetime, tolerance_minutes: int = 20) -> float | None:
    """Verilen ana en yakin ornekten piyasa degeri.

    En yakin kaydi SQL'de degil Python'da seciyoruz: tarih farki alan
    fonksiyonlar SQLite ve Postgres'te ayni degil, pencere de zaten kucuk.
    """
    lo = when - timedelta(minutes=tolerance_minutes)
    hi = when + timedelta(minutes=tolerance_minutes)
    rows = list(
        session.execute(
            select(PriceSnapshot.mc_usd, PriceSnapshot.at).where(
                PriceSnapshot.token_id == token_id,
                PriceSnapshot.at >= lo,
                PriceSnapshot.at <= hi,
                PriceSnapshot.mc_usd.is_not(None),
            )
        )
    )
    if not rows:
        return None
    mc, _ = min(rows, key=lambda r: abs((r[1] - when).total_seconds()))
    return mc


def prune(session: Session) -> dict[str, int]:
    """Eski ornekleri ve olu tokenleri temizler. Bedava diskte yasamak icin."""
    cutoff = utcnow() - timedelta(days=settings.snapshot_retention_days)
    watched = watched_token_ids(session)

    n_snap = session.query(PriceSnapshot).filter(PriceSnapshot.at < cutoff).delete(
        synchronize_session=False
    )
    # Uzun suredir olu, izlenmeyen ve hic alarm uretmemis tokenleri pasife al
    stale = utcnow() - timedelta(days=7)
    q = select(Token).where(
        Token.active.is_(True),
        Token.last_checked_at < stale,
    )
    n_off = 0
    for tok in session.scalars(q):
        if tok.id in watched:
            continue
        tok.active = False
        n_off += 1
    return {"silinen_ornek": int(n_snap or 0), "pasife_alinan_token": n_off}
