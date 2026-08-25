"""ALARM ISI: yuksek skorlu hesaplardan gelen TAZE cagrilari aninda bildirir.

Sistemin nihai urunu bu: gecmis performansiyla kanitlanmis hesaplar
yeni bir CA paylastigi anda telefonuna dusen mesaj.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..db.models import Account, AccountScore, Alert, Call, Token, Tweet, utcnow
from ..db.session import session_scope
from ..http import HttpClient
from ..notify.telegram import TIER_EMOJI, TelegramNotifier, esc, fmt_mult, fmt_usd

log = logging.getLogger(__name__)


def _already_sent(session, kind: str, key: str) -> bool:
    return session.scalar(
        select(func.count(Alert.id)).where(Alert.kind == kind, Alert.dedupe_key == key)
    ) > 0


def _record(session, kind: str, key: str, payload: dict) -> None:
    session.add(Alert(kind=kind, dedupe_key=key, payload=payload, delivered=True))
    try:
        session.flush()
    except IntegrityError:
        session.rollback()


async def alert_fresh_calls(lookback_minutes: int = 30) -> int:
    notifier = TelegramNotifier()
    if not notifier.enabled:
        return 0

    since = utcnow() - timedelta(minutes=lookback_minutes)
    messages: list[tuple[str, str, dict]] = []

    with session_scope() as s:
        latest = (
            select(AccountScore.account_id, func.max(AccountScore.computed_at).label("mx"))
            .group_by(AccountScore.account_id)
            .subquery()
        )
        stmt = (
            select(Call, Account, Token, Tweet, AccountScore)
            .join(Account, Call.account_id == Account.id)
            .join(Token, Call.token_id == Token.id)
            .join(Tweet, Call.tweet_id == Tweet.id)
            .join(AccountScore, AccountScore.account_id == Account.id)
            .join(
                latest,
                (AccountScore.account_id == latest.c.account_id)
                & (AccountScore.computed_at == latest.c.mx),
            )
            .where(
                Call.called_at >= since,
                AccountScore.alpha_score >= settings.alert_min_alpha_score,
                Account.is_blacklisted == False,  # noqa: E712
            )
            .order_by(AccountScore.alpha_score.desc())
            .limit(30)
        )
        for call, acc, tok, tw, sc in s.execute(stmt):
            key = f"{call.id}"
            if _already_sent(s, "fresh_call", key):
                continue
            if call.entry_mc_usd and call.entry_mc_usd > settings.alert_max_entry_mc_usd:
                continue
            if tok.security_score is not None and tok.security_score < 0.3:
                continue
            messages.append((key, _format_call(call, acc, tok, tw, sc), {"call_id": call.id}))

    if not messages:
        return 0

    sent = 0
    async with HttpClient() as http:
        for key, text, payload in messages:
            if await notifier.send(text, http=http):
                sent += 1
                with session_scope() as s:
                    _record(s, "fresh_call", key, payload)
    log.info("%d taze cagri alarmi gonderildi", sent)
    return sent


def _format_call(call, acc, tok, tw, sc) -> str:
    tier = sc.tier.value if hasattr(sc.tier, "value") else str(sc.tier)
    lines = [
        f"{TIER_EMOJI.get(tier, '⚪')} <b>{tier} TIER CAGRI</b>",
        f"👤 <a href=\"https://x.com/{esc(acc.handle)}\">@{esc(acc.handle)}</a>"
        f"  ·  alfa <b>{sc.alpha_score:.1f}</b>",
        f"📊 win {sc.win_rate*100:.0f}% ({sc.n_wins}/{sc.n_evaluated})"
        f"  ·  medyan {fmt_mult(sc.median_multiple)}"
        f"  ·  {sc.calls_per_day:.1f} cagri/gun",
        "",
        f"🪙 <b>{esc(tok.symbol or 'TOKEN')}</b> <code>{esc(tok.address)}</code>",
        f"⛓ {esc(tok.chain)}  ·  MC {fmt_usd(call.entry_mc_usd)}"
        f"  ·  likidite {fmt_usd(call.entry_liquidity_usd)}",
    ]
    if tok.security_score is not None:
        lines.append(f"🛡 guvenlik {tok.security_score*100:.0f}/100")
    if call.token_age_at_call_sec is not None:
        age_h = call.token_age_at_call_sec / 3600
        lines.append(f"⏱ token yasi {age_h:.1f} saat" if age_h < 48 else f"⏱ token yasi {age_h/24:.1f} gun")
    if call.caller_rank:
        lines.append(f"🥇 bu CA'yi cagiran {call.caller_rank}. hesap")
    lines += [
        "",
        f"🔗 <a href=\"{esc(tw.url or '')}\">tweet</a>"
        f" · <a href=\"https://dexscreener.com/{esc(tok.chain)}/{esc(tok.address)}\">dexscreener</a>",
    ]
    return "\n".join(lines)


async def send_leaderboard(limit: int = 15) -> bool:
    notifier = TelegramNotifier()
    if not notifier.enabled:
        return False

    with session_scope() as s:
        latest = (
            select(AccountScore.account_id, func.max(AccountScore.computed_at).label("mx"))
            .group_by(AccountScore.account_id)
            .subquery()
        )
        rows = list(
            s.execute(
                select(AccountScore, Account)
                .join(Account, AccountScore.account_id == Account.id)
                .join(
                    latest,
                    (AccountScore.account_id == latest.c.account_id)
                    & (AccountScore.computed_at == latest.c.mx),
                )
                .where(Account.is_blacklisted == False)  # noqa: E712
                .order_by(AccountScore.alpha_score.desc())
                .limit(limit)
            )
        )
        if not rows:
            return False
        lines = [f"🏆 <b>ALPHA HUNTER — TOP {len(rows)}</b>", ""]
        for i, (sc, acc) in enumerate(rows, 1):
            tier = sc.tier.value if hasattr(sc.tier, "value") else str(sc.tier)
            lines.append(
                f"{i:2}. {TIER_EMOJI.get(tier,'⚪')} <a href=\"https://x.com/{esc(acc.handle)}\">"
                f"@{esc(acc.handle)}</a> — <b>{sc.alpha_score:.1f}</b>"
            )
            lines.append(
                f"     win {sc.win_rate*100:.0f}% ({sc.n_wins}/{sc.n_evaluated}) · "
                f"medyan {fmt_mult(sc.median_multiple)} · giris {fmt_usd(sc.avg_entry_mc_usd)} · "
                f"{sc.calls_per_day:.1f}/gun"
            )
        text = "\n".join(lines)

    return await notifier.send(text, silent=True)
