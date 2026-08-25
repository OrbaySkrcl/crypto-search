"""Gecmise donuk analiz: bilinen bir hesabi test etmek icin.

'Su hesap gercekten iyi mi?' sorusunun cevabi. Hesabin son N gunluk
zaman cizelgesini ceker, cagrilarini bulur ve hepsini fiyatlandirir.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from ..db.models import Account
from ..db.session import session_scope
from ..scoring.engine import ScoreResult, persist_score, score_account
from .enrich import run_enrich
from .ingest import run_ingest

log = logging.getLogger(__name__)


async def backfill_account(
    handle: str, days: int = 60, enrich_rounds: int = 6
) -> tuple[ScoreResult | None, dict]:
    """(skor, ingest istatistikleri) doner.

    Istatistikler cagiranin "neden bulunamadi" sorusunu cevaplayabilmesi icin
    lazim: hic tweet mi cekilemedi, tweet var ama CA mi yok, yoksa CA var ama
    takip edilmeyen bir zincirde mi?
    """
    handle = handle.lstrip("@").lower()
    log.info("@%s icin %d gunluk gecmis cekiliyor", handle, days)

    stats = await run_ingest(lookback_minutes=days * 24 * 60, queries=[], handles=[handle])
    log.info("ingest: %s", stats)

    for i in range(enrich_rounds):
        res = await run_enrich(batch_size=40, security_budget=10)
        log.info("enrich turu %d/%d: %s", i + 1, enrich_rounds, res)
        if res["evaluated"] == 0:
            break

    with session_scope() as s:
        acc = s.scalar(select(Account).where(Account.handle == handle))
        if acc is None:
            log.warning("@%s icin hicbir cagri bulunamadi", handle)
            return (None, stats)
        result = score_account(s, acc)
        persist_score(s, result)
        return (result, stats)


async def backfill_many(handles: list[str], days: int = 60) -> list[ScoreResult]:
    out = []
    for h in handles:
        r, _stats = await backfill_account(h, days=days)
        if r:
            out.append(r)
    out.sort(key=lambda r: r.alpha_score, reverse=True)
    return out
