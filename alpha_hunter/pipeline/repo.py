"""DB upsert yardimcilari. Tum yazma islemleri buradan gecer."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db.models import (
    Account,
    Call,
    CallOutcome,
    PriceSnapshot,
    Token,
    TokenStatus,
    Tweet,
    utcnow,
)
from ..ingest.base import RawTweet
from ..oracle.types import TokenInfo
from ..scoring import metrics as M

log = logging.getLogger(__name__)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite timezone bilgisini dusurur; her okumada UTC'ye geri baglariz."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def upsert_account(session: Session, raw: RawTweet) -> Account:
    acc = session.scalar(
        select(Account).where(Account.platform == "x", Account.handle == raw.handle)
    )
    if acc is None:
        acc = Account(
            platform="x",
            handle=raw.handle,
            platform_user_id=raw.platform_user_id,
            display_name=raw.display_name,
            followers=raw.followers,
            following=raw.following,
            account_created_at=raw.account_created_at,
        )
        session.add(acc)
        session.flush()
    else:
        if raw.followers is not None:
            acc.followers = raw.followers
        if raw.following is not None:
            acc.following = raw.following
        if raw.display_name:
            acc.display_name = raw.display_name
        if raw.platform_user_id and not acc.platform_user_id:
            acc.platform_user_id = raw.platform_user_id
        acc.last_seen_at = utcnow()
    return acc


def upsert_tweet(session: Session, raw: RawTweet, account: Account) -> tuple[Tweet, bool]:
    tw = session.scalar(select(Tweet).where(Tweet.platform_tweet_id == raw.tweet_id))
    if tw is not None:
        # Daha zengin bir kaynaktan geldiyse metrikleri guncelle
        for fld in ("like_count", "retweet_count", "reply_count", "view_count"):
            v = getattr(raw, fld)
            if v is not None:
                setattr(tw, fld, v)
        if len(raw.text or "") > len(tw.text or ""):
            tw.text = raw.text
        return (tw, False)

    tw = Tweet(
        account_id=account.id,
        platform_tweet_id=raw.tweet_id,
        posted_at=raw.posted_at,
        text=raw.text,
        url=raw.url,
        lang=raw.lang,
        is_retweet=raw.is_retweet,
        is_reply=raw.is_reply,
        is_quote=raw.is_quote,
        like_count=raw.like_count,
        retweet_count=raw.retweet_count,
        reply_count=raw.reply_count,
        view_count=raw.view_count,
        source=raw.source,
        raw={"expanded_urls": raw.expanded_urls} if raw.expanded_urls else None,
    )
    session.add(tw)
    session.flush()
    return (tw, True)


def get_token(session: Session, chain: str, address: str) -> Token | None:
    return session.scalar(
        select(Token).where(Token.chain == chain, Token.address == address)
    )


def upsert_token(
    session: Session, chain: str, address: str, info: TokenInfo | None = None
) -> Token:
    tok = get_token(session, chain, address)
    if tok is None:
        tok = Token(chain=chain, address=address)
        session.add(tok)
        session.flush()
    if info:
        apply_token_info(tok, info)
    return tok


def apply_token_info(tok: Token, info: TokenInfo) -> None:
    tok.symbol = info.symbol or tok.symbol
    tok.name = info.name or tok.name
    tok.pair_address = info.pair_address or tok.pair_address
    tok.dex_id = info.dex_id or tok.dex_id
    tok.pair_created_at = info.pair_created_at or tok.pair_created_at
    if info.supply_estimate:
        tok.supply_estimate = info.supply_estimate
    if info.mc_usd:
        tok.last_mc_usd = info.mc_usd
        if not tok.ath_mc_usd or info.mc_usd > tok.ath_mc_usd:
            tok.ath_mc_usd = info.mc_usd
            tok.ath_mc_at = utcnow()
    if info.liquidity_usd is not None:
        tok.last_liquidity_usd = info.liquidity_usd
        if not tok.peak_liquidity_usd or info.liquidity_usd > tok.peak_liquidity_usd:
            tok.peak_liquidity_usd = info.liquidity_usd
    if tok.status in (TokenStatus.UNKNOWN, TokenStatus.INVALID):
        tok.status = TokenStatus.ACTIVE
    tok.last_refreshed_at = utcnow()


def mark_token_invalid(session: Session, chain: str, address: str) -> Token:
    tok = upsert_token(session, chain, address)
    tok.status = TokenStatus.INVALID
    tok.last_refreshed_at = utcnow()
    return tok


def add_snapshot(session: Session, token: Token, info: TokenInfo, ts: datetime | None = None) -> None:
    """Kendi fiyat gecmisimizi biriktiririz -- ucuncu taraf API'lerden bagimsiz."""
    if info.price_usd is None:
        return
    ts = (ts or utcnow()).replace(second=0, microsecond=0)
    exists = session.scalar(
        select(PriceSnapshot).where(
            PriceSnapshot.token_id == token.id,
            PriceSnapshot.ts == ts,
            PriceSnapshot.source == "dexscreener_live",
        )
    )
    if exists:
        return
    session.add(
        PriceSnapshot(
            token_id=token.id,
            ts=ts,
            price_usd=info.price_usd,
            high_usd=info.price_usd,
            low_usd=info.price_usd,
            mc_usd=info.mc_usd,
            liquidity_usd=info.liquidity_usd,
            volume_usd=info.volume_24h,
            source="dexscreener_live",
        )
    )


def create_call(
    session: Session, account: Account, token: Token, tweet: Tweet, chain: str
) -> tuple[Call, bool]:
    existing = session.scalar(
        select(Call).where(Call.tweet_id == tweet.id, Call.token_id == token.id)
    )
    if existing:
        return (existing, False)
    call = Call(
        account_id=account.id,
        token_id=token.id,
        tweet_id=tweet.id,
        chain=chain,
        called_at=tweet.posted_at,
    )
    session.add(call)
    session.flush()
    return (call, True)


def refresh_caller_ranks(session: Session, token_id: int) -> None:
    """ECHO TESPITI: ayni tokeni cagiranlari zamana gore siralar.

    1. sirada olan = ilk kesfeden (originality 1.0). Sonrakiler gecikmeye gore
    ustel ceza alir -- 'copycat' bacaginin sayisal karsiligi.
    """
    calls = list(
        session.scalars(
            select(Call).where(Call.token_id == token_id).order_by(Call.called_at.asc())
        )
    )
    if not calls:
        return
    first_at = _aware(calls[0].called_at)
    seen_accounts: set[int] = set()
    rank = 0
    for c in calls:
        if c.account_id not in seen_accounts:
            seen_accounts.add(c.account_id)
            rank += 1
        c.caller_rank = rank
        delay = int((_aware(c.called_at) - first_at).total_seconds())
        c.echo_delay_sec = max(0, delay)
        c.originality = M.originality(c.echo_delay_sec, settings.echo_tau_seconds)


def classify_outcome(call: Call, token: Token) -> tuple[CallOutcome, str | None]:
    """Bir cagriyi WIN / LOSS / RUG / INVALID olarak etiketler."""
    if call.entry_price_usd is None:
        return (CallOutcome.PENDING, None)

    liq = call.entry_liquidity_usd
    if liq is not None and liq < settings.min_entry_liquidity_usd:
        return (CallOutcome.INVALID, f"giris likiditesi ${liq:,.0f} < esik")
    if call.entry_mc_usd and call.entry_mc_usd > settings.max_entry_mc_usd:
        return (CallOutcome.INVALID, "giris MC ust sinirin uzerinde")

    mult = call.sustained_multiple or call.max_multiple or 0.0
    if mult >= settings.win_multiple:
        # Zirveye ULASTIKTAN sonra rug olduysa cagri yine de basarilidir;
        # zirveden ONCE rug olduysa kayiptir.
        return (CallOutcome.WIN, None)
    if token.status == TokenStatus.RUGGED:
        return (CallOutcome.RUG, "token rug oldu")
    return (CallOutcome.LOSS, None)
