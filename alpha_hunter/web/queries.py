"""Web arayuzunun okudugu sorgular. Yalnizca OKUMA yapar."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db.models import (
    Account,
    AccountCluster,
    AccountScore,
    Call,
    Token,
    Tweet,
    utcnow,
)


def _latest_score_subq():
    return (
        select(AccountScore.account_id, func.max(AccountScore.computed_at).label("mx"))
        .group_by(AccountScore.account_id)
        .subquery()
    )


def leaderboard(session: Session, limit: int = 50, min_tier: str | None = None) -> list[dict]:
    sub = _latest_score_subq()
    stmt = (
        select(AccountScore, Account)
        .join(Account, AccountScore.account_id == Account.id)
        .join(
            sub,
            (AccountScore.account_id == sub.c.account_id)
            & (AccountScore.computed_at == sub.c.mx),
        )
        .order_by(AccountScore.alpha_score.desc())
        .limit(limit)
    )
    order = ["F", "D", "C", "B", "A", "S"]
    allowed = set(order[order.index(min_tier):]) if min_tier in order else None

    out: list[dict] = []
    for sc, acc in session.execute(stmt):
        tier = sc.tier.value if hasattr(sc.tier, "value") else str(sc.tier)
        if allowed and tier not in allowed:
            continue
        out.append(
            {
                "handle": acc.handle,
                "followers": acc.followers,
                "tier": tier,
                "alpha_score": round(sc.alpha_score, 1),
                "win_rate": round(sc.win_rate, 3),
                "n_wins": sc.n_wins,
                "n_evaluated": sc.n_evaluated,
                "n_moons": sc.n_moons,
                "n_rugs": sc.n_rugs,
                "median_multiple": round(sc.median_multiple, 2),
                "p90_multiple": round(sc.p90_multiple, 2),
                "avg_entry_mc": sc.avg_entry_mc_usd,
                "entry_quality": round(sc.entry_quality, 3),
                "originality": round(sc.originality, 3),
                "survivorship": round(sc.survivorship, 3),
                "consistency": round(sc.consistency, 3),
                "calls_per_day": round(sc.calls_per_day, 2),
                "spray_penalty": round(sc.spray_penalty, 3),
                "wilson_lb": round(sc.wilson_lb, 3),
                "data_confidence": round(sc.data_confidence, 3),
                "blacklisted": acc.is_blacklisted,
                "blacklist_reason": acc.blacklist_reason,
                "clustered": acc.cluster_id is not None,
                "computed_at": sc.computed_at.isoformat(),
            }
        )
    return out


def account_detail(session: Session, handle: str, limit: int = 60) -> dict | None:
    handle = handle.lstrip("@").lower()
    acc = session.scalar(select(Account).where(Account.handle == handle))
    if acc is None:
        return None

    sc = session.scalar(
        select(AccountScore)
        .where(AccountScore.account_id == acc.id)
        .order_by(AccountScore.computed_at.desc())
        .limit(1)
    )
    cluster = session.get(AccountCluster, acc.cluster_id) if acc.cluster_id else None

    calls = []
    stmt = (
        select(Call, Token, Tweet)
        .join(Token, Call.token_id == Token.id)
        .join(Tweet, Call.tweet_id == Tweet.id)
        .where(Call.account_id == acc.id)
        .order_by(Call.called_at.desc())
        .limit(limit)
    )
    for call, tok, tw in session.execute(stmt):
        calls.append(
            {
                "called_at": call.called_at.isoformat(),
                "chain": call.chain,
                "symbol": tok.symbol,
                "address": tok.address,
                "entry_mc": call.entry_mc_usd,
                "entry_liquidity": call.entry_liquidity_usd,
                "entry_confidence": round(call.entry_confidence or 0, 2),
                "max_multiple": call.max_multiple,
                "sustained_multiple": call.sustained_multiple,
                "entry_quality": call.entry_quality,
                "run_capture": call.run_capture,
                "caller_rank": call.caller_rank,
                "outcome": call.outcome.value if hasattr(call.outcome, "value") else str(call.outcome),
                "tweet_url": tw.url,
                "security_score": tok.security_score,
            }
        )

    return {
        "handle": acc.handle,
        "display_name": acc.display_name,
        "followers": acc.followers,
        "blacklisted": acc.is_blacklisted,
        "blacklist_reason": acc.blacklist_reason,
        "cluster": cluster.label if cluster else None,
        "score": leaderboard_row_from(sc) if sc else None,
        "calls": calls,
    }


def leaderboard_row_from(sc: AccountScore) -> dict:
    return {
        "tier": sc.tier.value if hasattr(sc.tier, "value") else str(sc.tier),
        "alpha_score": round(sc.alpha_score, 1),
        "win_rate": round(sc.win_rate, 3),
        "n_wins": sc.n_wins,
        "n_evaluated": sc.n_evaluated,
        "median_multiple": round(sc.median_multiple, 2),
        "entry_quality": round(sc.entry_quality, 3),
        "originality": round(sc.originality, 3),
        "survivorship": round(sc.survivorship, 3),
        "consistency": round(sc.consistency, 3),
        "wilson_lb": round(sc.wilson_lb, 3),
        "calls_per_day": round(sc.calls_per_day, 2),
        "spray_penalty": round(sc.spray_penalty, 3),
        "breakdown": sc.breakdown,
    }


def recent_calls(session: Session, hours: int = 24, limit: int = 100, min_alpha: float = 0.0) -> list[dict]:
    since = utcnow() - timedelta(hours=hours)
    sub = _latest_score_subq()
    stmt = (
        select(Call, Account, Token, Tweet, AccountScore)
        .join(Account, Call.account_id == Account.id)
        .join(Token, Call.token_id == Token.id)
        .join(Tweet, Call.tweet_id == Tweet.id)
        .outerjoin(sub, sub.c.account_id == Account.id)
        .outerjoin(
            AccountScore,
            (AccountScore.account_id == sub.c.account_id)
            & (AccountScore.computed_at == sub.c.mx),
        )
        .where(Call.called_at >= since)
        .order_by(Call.called_at.desc())
        .limit(limit)
    )
    out = []
    for call, acc, tok, tw, sc in session.execute(stmt):
        alpha = sc.alpha_score if sc else 0.0
        if alpha < min_alpha:
            continue
        out.append(
            {
                "called_at": call.called_at.isoformat(),
                "handle": acc.handle,
                "alpha_score": round(alpha, 1),
                "tier": (sc.tier.value if sc and hasattr(sc.tier, "value") else "UNRATED"),
                "chain": call.chain,
                "symbol": tok.symbol,
                "address": tok.address,
                "entry_mc": call.entry_mc_usd,
                "max_multiple": call.max_multiple,
                "sustained_multiple": call.sustained_multiple,
                "caller_rank": call.caller_rank,
                "outcome": call.outcome.value if hasattr(call.outcome, "value") else str(call.outcome),
                "security_score": tok.security_score,
                "tweet_url": tw.url,
            }
        )
    return out


def overview(session: Session) -> dict:
    def count(model) -> int:
        return session.scalar(select(func.count()).select_from(model)) or 0

    outcomes = {
        (k.value if hasattr(k, "value") else str(k)): v
        for k, v in session.execute(
            select(Call.outcome, func.count(Call.id)).group_by(Call.outcome)
        ).all()
    }
    best = session.scalar(select(func.max(Call.max_multiple)))
    last_call = session.scalar(select(func.max(Call.called_at)))
    tiers: dict[str, int] = {}
    for row in leaderboard(session, limit=10_000):
        tiers[row["tier"]] = tiers.get(row["tier"], 0) + 1

    return {
        "accounts": count(Account),
        "tweets": count(Tweet),
        "tokens": count(Token),
        "calls": count(Call),
        "outcomes": outcomes,
        "tiers": tiers,
        "best_multiple": best,
        "calls_24h": session.scalar(
            select(func.count(Call.id)).where(Call.called_at >= utcnow() - timedelta(hours=24))
        ) or 0,
        "last_call_at": last_call.isoformat() if last_call else None,
        "chains": settings.chain_list,
        "window_days": settings.score_window_days,
        "win_multiple": settings.win_multiple,
        "server_time": utcnow().isoformat(),
    }
