"""Skorlama motoru: DB'deki call'lardan hesap skorlari uretir."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db.models import Account, AccountScore, Call, CallOutcome, Tier, utcnow
from . import market as MK
from . import metrics as M

log = logging.getLogger(__name__)


@dataclass
class ScoreResult:
    account_id: int
    handle: str
    alpha_score: float
    tier: str
    n_calls: int = 0
    n_evaluated: int = 0
    n_wins: int = 0
    n_moons: int = 0
    n_rugs: int = 0
    win_rate: float = 0.0
    wilson_lb: float = 0.0
    median_multiple: float = 0.0
    p90_multiple: float = 0.0
    avg_entry_mc: float | None = None
    magnitude: float = 0.0
    market_edge: float = 0.0
    median_excess: float = 0.0
    tradeability: float = 0.0
    entry_quality: float = 0.0
    survivorship: float = 0.0
    originality: float = 0.0
    consistency: float = 0.0
    calls_per_day: float = 0.0
    spray_penalty: float = 1.0
    data_confidence: float = 0.0
    breakdown: dict = field(default_factory=dict)


WEIGHTS = {
    "reliability": settings.w_reliability,
    "magnitude": settings.w_magnitude,
    "market_edge": settings.w_market_edge,
    "entry_quality": settings.w_entry_quality,
    "survivorship": settings.w_survivorship,
    "originality": settings.w_originality,
}


def score_account(session: Session, account: Account, now: datetime | None = None) -> ScoreResult:
    now = now or utcnow()
    since = now - timedelta(days=settings.score_window_days)

    calls: list[Call] = list(
        session.scalars(
            select(Call).where(Call.account_id == account.id, Call.called_at >= since)
        )
    )

    res = ScoreResult(
        account_id=account.id, handle=account.handle, alpha_score=0.0, tier=Tier.UNRATED.value
    )
    res.n_calls = len(calls)
    if not calls:
        return res

    # --- Cagri frekansi: KURAL 2 (spray & pray) --------------------------- #
    # Ayni tokene ayni gun 5 tweet atmak 1 cagridir; tekil (gun, token) sayilir.
    unique_daily = {(c.called_at.date(), c.token_id) for c in calls}
    first_call = min(c.called_at for c in calls)
    active_days = max(1.0, (now - max(first_call, since)).total_seconds() / 86400.0)
    res.calls_per_day = len(unique_daily) / active_days
    res.spray_penalty = M.spray_penalty(
        res.calls_per_day,
        settings.spray_soft_calls_per_day,
        settings.spray_hard_calls_per_day,
        settings.spray_min_penalty,
    )

    # --- Degerlendirilebilir cagrilar ------------------------------------- #
    scored = [c for c in calls if c.outcome in (CallOutcome.WIN, CallOutcome.LOSS, CallOutcome.RUG)]
    res.n_evaluated = len(scored)
    if not scored:
        res.breakdown = {"note": "degerlendirilmis cagri yok (fiyat verisi bekleniyor)"}
        return res

    # Zaman agirliklari: eski basarilar daha az deger eder
    weights = [M.recency_weight(c.called_at, now, settings.score_halflife_days) for c in scored]
    # Fiyat cozunurlugu dusuk olan cagrilar da daha az agirlik alir
    weights = [w * (0.4 + 0.6 * (c.entry_confidence or 0.0)) for w, c in zip(weights, scored, strict=True)]

    wins = [c for c in scored if c.outcome == CallOutcome.WIN]
    rugs = [c for c in scored if c.outcome == CallOutcome.RUG]
    res.n_wins = len(wins)
    res.n_rugs = len(rugs)
    res.n_moons = sum(
        1 for c in scored if (c.sustained_multiple or 0.0) >= settings.moon_multiple
    )
    res.win_rate = res.n_wins / res.n_evaluated

    w_success = sum(w for w, c in zip(weights, scored, strict=True) if c.outcome == CallOutcome.WIN)
    w_total = sum(weights)
    res.wilson_lb = M.wilson_lower_bound(w_success, w_total)

    # --- Buyukluk --------------------------------------------------------- #
    mults = [max(c.sustained_multiple or c.max_multiple or 0.0, 1e-6) for c in scored]
    res.median_multiple = M.weighted_median(mults, weights)
    res.p90_multiple = M.weighted_quantile(mults, weights, 0.90)
    res.magnitude = M.magnitude_score(mults, weights, settings.moon_multiple)

    # --- Piyasa cipasi: kohortu gecti mi? --------------------------------- #
    ex_pairs = [
        (c.excess_multiple, w)
        for c, w in zip(scored, weights, strict=True)
        if c.excess_multiple is not None
    ]
    if ex_pairs:
        res.median_excess = M.weighted_median(
            [v for v, _ in ex_pairs], [w for _, w in ex_pairs]
        )
        res.market_edge = MK.market_edge_score(
            [v for v, _ in ex_pairs], [w for _, w in ex_pairs]
        )

    # --- Alinabilirlik: o fiyattan gercekten girilebilir miydi? ------------ #
    tr_pairs = [
        (c.tradeability, w)
        for c, w in zip(scored, weights, strict=True)
        if c.tradeability is not None
    ]
    if tr_pairs:
        tot = sum(w for _, w in tr_pairs) or 1.0
        res.tradeability = sum(v * w for v, w in tr_pairs) / tot

    # --- Giris kalitesi: KURAL 1 ------------------------------------------ #
    eq_pairs = [(c.entry_quality, w) for c, w in zip(scored, weights, strict=True) if c.entry_quality is not None]
    if eq_pairs:
        tot = sum(w for _, w in eq_pairs) or 1.0
        res.entry_quality = sum(v * w for v, w in eq_pairs) / tot
    entry_mcs = [c.entry_mc_usd for c in scored if c.entry_mc_usd]
    res.avg_entry_mc = (sum(entry_mcs) / len(entry_mcs)) if entry_mcs else None

    # --- Hayatta kalma (rug orani) ---------------------------------------- #
    w_rug = sum(w for w, c in zip(weights, scored, strict=True) if c.outcome == CallOutcome.RUG)
    res.survivorship = M.clamp(1.0 - (w_rug / w_total if w_total else 0.0))

    # --- Ozgunluk (echo) --------------------------------------------------- #
    orig_pairs = [(c.originality, w) for c, w in zip(scored, weights, strict=True) if c.originality is not None]
    if orig_pairs:
        tot = sum(w for _, w in orig_pairs) or 1.0
        res.originality = sum(v * w for v, w in orig_pairs) / tot
    else:
        res.originality = 0.5

    # --- Tutarlilik -------------------------------------------------------- #
    res.consistency = M.consistency([c.called_at for c in scored], [c.called_at for c in wins])

    # --- Veri guveni ------------------------------------------------------- #
    res.data_confidence = M.clamp(res.n_evaluated / (settings.min_calls_for_rating * 3.0))

    res.alpha_score = M.composite_alpha(
        reliability=res.wilson_lb,
        magnitude=res.magnitude,
        market_edge=res.market_edge,
        entry_q=res.entry_quality,
        survivorship=res.survivorship,
        original=res.originality,
        weights=WEIGHTS,
        spray=res.spray_penalty,
        consist=res.consistency,
        data_conf=res.data_confidence,
        tradeable=res.tradeability,
    )
    res.tier = M.tier_for(res.alpha_score, res.n_evaluated, settings.min_calls_for_rating)

    if account.is_blacklisted:
        res.alpha_score = 0.0
        res.tier = Tier.F.value

    res.breakdown = {
        "reliability_wilson": round(res.wilson_lb, 4),
        "magnitude": round(res.magnitude, 4),
        "market_edge": round(res.market_edge, 4),
        "median_excess": round(res.median_excess, 3),
        "tradeability": round(res.tradeability, 4),
        "entry_quality": round(res.entry_quality, 4),
        "survivorship": round(res.survivorship, 4),
        "originality": round(res.originality, 4),
        "spray_penalty": round(res.spray_penalty, 4),
        "consistency": round(res.consistency, 4),
        "data_confidence": round(res.data_confidence, 4),
        "weights": WEIGHTS,
        "window_days": settings.score_window_days,
    }
    return res


def persist_score(session: Session, res: ScoreResult, now: datetime | None = None) -> AccountScore:
    now = now or utcnow()
    row = AccountScore(
        account_id=res.account_id,
        computed_at=now,
        window_days=settings.score_window_days,
        n_calls=res.n_calls,
        n_evaluated=res.n_evaluated,
        n_wins=res.n_wins,
        n_moons=res.n_moons,
        n_rugs=res.n_rugs,
        win_rate=res.win_rate,
        wilson_lb=res.wilson_lb,
        median_multiple=res.median_multiple,
        p90_multiple=res.p90_multiple,
        avg_entry_mc_usd=res.avg_entry_mc,
        magnitude=res.magnitude,
        market_edge=res.market_edge,
        median_excess=res.median_excess,
        tradeability=res.tradeability,
        entry_quality=res.entry_quality,
        survivorship=res.survivorship,
        originality=res.originality,
        consistency=res.consistency,
        calls_per_day=res.calls_per_day,
        spray_penalty=res.spray_penalty,
        data_confidence=res.data_confidence,
        alpha_score=res.alpha_score,
        tier=Tier(res.tier),
        breakdown=res.breakdown,
    )
    session.add(row)
    return row


def score_all(session: Session, min_calls: int = 1) -> list[ScoreResult]:
    now = utcnow()
    since = now - timedelta(days=settings.score_window_days)
    ids = session.scalars(
        select(Call.account_id)
        .where(Call.called_at >= since)
        .group_by(Call.account_id)
        .having(func.count(Call.id) >= min_calls)
    ).all()

    out: list[ScoreResult] = []
    for aid in ids:
        acc = session.get(Account, aid)
        if acc is None:
            continue
        res = score_account(session, acc, now=now)
        persist_score(session, res, now=now)
        out.append(res)
    session.flush()
    out.sort(key=lambda r: r.alpha_score, reverse=True)
    log.info("%d hesap skorlandi", len(out))
    return out


def latest_scores(session: Session, limit: int = 50, min_tier: str | None = None) -> list[AccountScore]:
    """Her hesap icin en son skor satiri, alpha_score'a gore sirali."""
    sub = (
        select(AccountScore.account_id, func.max(AccountScore.computed_at).label("mx"))
        .group_by(AccountScore.account_id)
        .subquery()
    )
    stmt = (
        select(AccountScore)
        .join(sub, (AccountScore.account_id == sub.c.account_id) & (AccountScore.computed_at == sub.c.mx))
        .order_by(AccountScore.alpha_score.desc())
        .limit(limit)
    )
    rows = list(session.scalars(stmt))
    if min_tier:
        order = ["F", "D", "C", "B", "A", "S"]
        if min_tier in order:
            allowed = set(order[order.index(min_tier):])
            rows = [r for r in rows if r.tier.value in allowed]
    return rows
