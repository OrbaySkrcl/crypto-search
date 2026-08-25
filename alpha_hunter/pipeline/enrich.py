"""ENRICH ISI: bekleyen cagrilari on-chain gercekle yuzlestirir.

Her cagri icin:
  * T1 fiyati/MC'si (yoksa tarihsel OHLCV'den cozulur)
  * T1 oncesi tepe   -> copycat mi?
  * T1 sonrasi tepe, korunan tepe, pencere getirileri
  * rug / gudugu kalma durumu
  * WIN / LOSS / RUG etiketi

Yeniden degerlendirme takvimi kademelidir: taze cagrilar sik, eski cagrilar
seyrek kontrol edilir. Pencere kapaninca (varsayilan 7 gun) call dondurulur.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db.models import Call, CallOutcome, PriceSnapshot, Token, TokenStatus, utcnow
from ..db.session import session_scope
from ..http import HttpClient
from ..oracle.resolver import PriceOracle
from ..oracle.types import Candle, PriceHistory
from ..scoring import metrics as M
from ..security.checks import SecurityChecker, detect_rug
from . import repo

log = logging.getLogger(__name__)


def _due(call: Call, now: datetime) -> bool:
    last = repo._aware(call.last_evaluated_at)
    if last is None:
        return True
    age = (now - repo._aware(call.called_at)).total_seconds()
    # Kademeli: ilk saat 5 dk, ilk gun ~saatlik, sonrasi ~6 saatlik
    if age < 3600:
        interval = 300
    elif age < 86400:
        interval = 3600
    else:
        interval = 21600
    return (now - last).total_seconds() >= interval


def _history_from_db(session: Session, token: Token) -> PriceHistory:
    """Kendi biriktirdigimiz anlik olcumler -- ucuncu taraf API cokse bile kalir."""
    hist = PriceHistory(chain=token.chain, address=token.address)
    rows = list(
        session.scalars(
            select(PriceSnapshot)
            .where(PriceSnapshot.token_id == token.id)
            .order_by(PriceSnapshot.ts.asc())
        )
    )
    if rows:
        hist.merge(
            [
                Candle(
                    repo._aware(r.ts), r.price_usd, r.high_usd or r.price_usd,
                    r.low_usd or r.price_usd, r.price_usd, r.volume_usd or 0.0, 60,
                )
                for r in rows
            ],
            "own_snapshots",
        )
    return hist


async def run_enrich(batch_size: int = 60, security_budget: int = 25) -> dict:
    now = datetime.now(timezone.utc)
    stats = {"evaluated": 0, "closed": 0, "wins": 0, "rugs": 0, "invalid": 0, "security_checked": 0}

    with session_scope() as s:
        pending = list(
            s.scalars(
                select(Call)
                .where(Call.is_closed == False)  # noqa: E712
                .order_by(Call.last_evaluated_at.is_(None).desc(), Call.called_at.desc())
                .limit(batch_size * 4)
            )
        )
        due = [c for c in pending if _due(c, now)][:batch_size]
        # Ayni token icin tek fiyat gecmisi cekelim
        work: list[tuple[int, int]] = [(c.id, c.token_id) for c in due]

    if not work:
        log.info("degerlendirilecek cagri yok")
        return stats

    by_token: dict[int, list[int]] = {}
    for call_id, token_id in work:
        by_token.setdefault(token_id, []).append(call_id)
    log.info("%d cagri / %d token degerlendiriliyor", len(work), len(by_token))

    async with HttpClient() as http:
        oracle = PriceOracle(http)
        sec = SecurityChecker(http)

        for token_id, call_ids in by_token.items():
            with session_scope() as s:
                token = s.get(Token, token_id)
                if token is None:
                    continue
                chain, address = token.chain, token.address
                own_hist = _history_from_db(s, token)
                need_security = (
                    token.security_checked_at is None
                    and chain == "solana"
                    and stats["security_checked"] < security_budget
                )

            info = await oracle.token_info(chain, address)
            if info is None:
                with session_scope() as s:
                    tok = s.get(Token, token_id)
                    if tok:
                        tok.status = TokenStatus.INVALID
                        tok.last_refreshed_at = utcnow()
                        for cid in call_ids:
                            c = s.get(Call, cid)
                            if c:
                                c.outcome = CallOutcome.INVALID
                                c.invalid_reason = "token DEX'te bulunamadi"
                                c.is_closed = True
                                c.last_evaluated_at = utcnow()
                                stats["invalid"] += 1
                continue

            # Fiyat gecmisini bir kez insa et, tum cagrilarda paylas
            with session_scope() as s:
                calls_meta = [
                    (c.id, repo._aware(c.called_at))
                    for c in s.scalars(select(Call).where(Call.id.in_(call_ids)))
                ]
            if not calls_meta:
                continue
            earliest = min(t for _, t in calls_meta)
            latest = max(t for _, t in calls_meta)
            birth = info.pair_created_at or (earliest - timedelta(days=30))
            hist = await oracle.build_history(
                chain,
                info,
                start=min(birth, earliest - timedelta(hours=6)),
                end=min(now, latest + timedelta(hours=settings.eval_close_after_hours)),
                focus=earliest,
            )
            hist.merge(own_hist.candles, "own_snapshots")

            rugged, rug_note = detect_rug(info.liquidity_usd, None)

            report = None
            if need_security:
                report = await sec.check(chain, address)
                stats["security_checked"] += 1

            with session_scope() as s:
                token = s.get(Token, token_id)
                if token is None:
                    continue
                repo.apply_token_info(token, info)
                rugged2, rug_note2 = detect_rug(token.last_liquidity_usd, token.peak_liquidity_usd)
                if rugged or rugged2:
                    token.status = TokenStatus.RUGGED
                if report:
                    token.security = report.as_dict()
                    token.security_score = report.score
                    token.security_checked_at = report.checked_at
                token.history_backfilled = bool(hist.candles)

                for cid in call_ids:
                    call = s.get(Call, cid)
                    if call is None:
                        continue
                    called_at = repo._aware(call.called_at)
                    ev = await oracle.evaluate(chain, address, called_at, token=info, history=hist)
                    call.last_evaluated_at = utcnow()
                    stats["evaluated"] += 1

                    if not ev.ok:
                        # Fiyat cozulemedi: pencere kapandiysa gecersiz say
                        if now >= called_at + timedelta(hours=settings.eval_close_after_hours):
                            call.outcome = CallOutcome.INVALID
                            call.invalid_reason = ev.reason or "fiyat cozulemedi"
                            call.is_closed = True
                            stats["invalid"] += 1
                        continue

                    _apply_evaluation(call, ev)
                    outcome, reason = repo.classify_outcome(call, token)
                    call.outcome = outcome
                    call.invalid_reason = reason
                    if outcome == CallOutcome.WIN:
                        stats["wins"] += 1
                    elif outcome == CallOutcome.RUG:
                        stats["rugs"] += 1
                    elif outcome == CallOutcome.INVALID:
                        stats["invalid"] += 1

                    if ev.window_closed and outcome != CallOutcome.PENDING:
                        call.is_closed = True
                        stats["closed"] += 1

                repo.refresh_caller_ranks(s, token_id)

    log.info("enrich bitti: %s", stats)
    return stats


def _apply_evaluation(call: Call, ev) -> None:
    # Giris fotografi: ingest sirasinda canli yakalanmissa ONA guveniriz
    if call.entry_confidence < (ev.entry_confidence or 0.0) or call.entry_price_usd is None:
        call.entry_price_usd = ev.entry_price_usd
        call.entry_mc_usd = ev.entry_mc_usd
        call.entry_liquidity_usd = ev.entry_liquidity_usd or call.entry_liquidity_usd
        call.entry_source = ev.entry_source
        call.entry_confidence = ev.entry_confidence
        call.entry_resolved_at = utcnow()

    call.pre_ath_mc_usd = ev.pre_ath_mc_usd
    call.pre_ath_at = ev.pre_ath_at
    call.token_age_at_call_sec = ev.token_age_at_call_sec

    call.max_mc_usd_after = ev.max_mc_usd_after
    call.max_mc_at = ev.max_mc_at
    call.max_drawdown_after = ev.max_drawdown_after
    call.mc_by_window = ev.mc_by_window or None
    call.multiple_by_window = ev.multiple_by_window or None

    # Katlar giris fiyatina gore YENIDEN hesaplanir: entry_price ingest'ten
    # gelmis olabilir ve oracle'inkinden daha dogrudur.
    if call.entry_price_usd and ev.entry_price_usd:
        scale = ev.entry_price_usd / call.entry_price_usd
        call.max_multiple = (ev.max_multiple or 0.0) * scale
        call.sustained_multiple = (ev.sustained_multiple or 0.0) * scale
    else:
        call.max_multiple = ev.max_multiple
        call.sustained_multiple = ev.sustained_multiple

    call.run_capture = ev.run_capture
    call.mc_earliness = (
        M.mc_earliness(call.entry_mc_usd, settings.mc_earliness_low_usd, settings.mc_earliness_high_usd)
        if call.entry_mc_usd else ev.mc_earliness
    )
    call.entry_quality = M.entry_quality(call.mc_earliness, call.run_capture)
