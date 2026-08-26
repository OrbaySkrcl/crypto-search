"""ASAMA 3: cuzdan <-> Twitter hesabi eslestirmesi.

Fikir tek cumlede: bir cuzdan surekli @x'in tweetinden HEMEN ONCE aliyorsa,
@x ya o cuzdanin sahibidir ya da ondan besleniyordur.

Bu artik korelasyon degil. Tek bir tokende tesaduf olabilir; ayni cuzdanin
ayni hesaptan once alma deseni bes ayri tokende tekrarlaniyorsa tesaduf degil.
Kullanicinin en bastan istedigi "insider tespiti" tam olarak budur.
"""
from __future__ import annotations

import logging
import statistics
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db.models import Account, Call, CallSource, Wallet
from ..pipeline import repo

log = logging.getLogger(__name__)


def find_links(
    session: Session,
    max_lead_seconds: int | None = None,
    min_tokens: int | None = None,
) -> list[dict]:
    """Tweet'ten once alan cuzdanlari bulur.

    Bir (cuzdan, hesap) ciftinin kanit sayilmasi icin:
      * cuzdan, hesabin tweetinden ONCE almis olmali (sonra alan takipcidir)
      * gecikme makul bir pencerede olmali (varsayilan 2 saat)
      * bu desen en az `min_tokens` FARKLI tokende tekrarlanmali
    """
    lead = max_lead_seconds or settings.link_max_lead_seconds
    need = min_tokens or settings.link_min_tokens

    wallet_calls = list(
        session.scalars(select(Call).where(Call.source == CallSource.WALLET))
    )
    tweet_calls = list(
        session.scalars(
            select(Call).where(Call.source == CallSource.TWEET, Call.account_id.isnot(None))
        )
    )
    if not wallet_calls or not tweet_calls:
        return []

    by_token: dict[int, list[Call]] = defaultdict(list)
    for c in tweet_calls:
        by_token[c.token_id].append(c)

    # (wallet_id, account_id) -> {token_id: en kisa gecikme}
    pairs: dict[tuple[int, int], dict[int, float]] = defaultdict(dict)
    for wc in wallet_calls:
        w_at = repo._aware(wc.called_at)
        for tc in by_token.get(wc.token_id, ()):
            delta = (repo._aware(tc.called_at) - w_at).total_seconds()
            if delta <= 0 or delta > lead:
                continue                      # tweet'ten sonra almis ya da cok uzak
            prev = pairs[(wc.wallet_id, tc.account_id)].get(wc.token_id)
            if prev is None or delta < prev:
                pairs[(wc.wallet_id, tc.account_id)][wc.token_id] = delta

    out: list[dict] = []
    for (wid, aid), tokens in pairs.items():
        if len(tokens) < need:
            continue
        leads = sorted(tokens.values())
        median_lead = statistics.median(leads)
        # Tutarli ve kisa gecikme daha guclu kanittir
        spread = (max(leads) - min(leads)) / max(median_lead, 1.0)
        confidence = _confidence(len(tokens), median_lead, spread, lead)
        out.append({
            "wallet_id": wid,
            "account_id": aid,
            "token_count": len(tokens),
            "median_lead_sec": int(median_lead),
            "min_lead_sec": int(min(leads)),
            "consistency": round(1.0 / (1.0 + spread), 3),
            "confidence": round(confidence, 3),
        })

    out.sort(key=lambda d: -d["confidence"])
    return out


def _confidence(n_tokens: int, median_lead: float, spread: float, window: float) -> float:
    """0..1.

    Uc sinyal: kac tokende tekrarlandi, gecikme ne kadar kisa, ne kadar tutarli.
    """
    from ..scoring.metrics import clamp

    # 3 token ~0.5, 8+ token ~1.0
    volume = clamp((n_tokens - 2) / 6.0)
    # 1 dakika onceden almak, 2 saat onceden almaktan cok daha anlamli
    speed = clamp(1.0 - (median_lead / max(window, 1.0)))
    steadiness = clamp(1.0 / (1.0 + spread))
    return clamp(0.45 * volume + 0.35 * speed + 0.20 * steadiness)


def apply_links(session: Session, links: list[dict], min_confidence: float | None = None) -> int:
    """Yeterince guclu eslesmeleri cuzdan kaydina yazar."""
    floor = min_confidence if min_confidence is not None else settings.link_min_confidence
    best: dict[int, dict] = {}
    for link in links:
        if link["confidence"] < floor:
            continue
        cur = best.get(link["wallet_id"])
        if cur is None or link["confidence"] > cur["confidence"]:
            best[link["wallet_id"]] = link

    n = 0
    for wid, link in best.items():
        w = session.get(Wallet, wid)
        if w is None:
            continue
        acc = session.get(Account, link["account_id"])
        w.linked_account_id = link["account_id"]
        w.link_confidence = link["confidence"]
        w.link_evidence = {
            "token_count": link["token_count"],
            "median_lead_sec": link["median_lead_sec"],
            "min_lead_sec": link["min_lead_sec"],
            "consistency": link["consistency"],
            "handle": acc.handle if acc else None,
        }
        if acc and not w.label:
            w.label = f"@{acc.handle} (muhtemel)"
        n += 1
    if n:
        log.info("%d cuzdan Twitter hesabiyla eslestirildi", n)
    return n


def run_linking(session: Session) -> list[dict]:
    links = find_links(session)
    apply_links(session, links)
    return links


def linked_summary(session: Session, limit: int = 25) -> list[dict]:
    rows = list(
        session.scalars(
            select(Wallet)
            .where(Wallet.linked_account_id.isnot(None))
            .order_by(Wallet.link_confidence.desc())
            .limit(limit)
        )
    )
    out = []
    for w in rows:
        acc = session.get(Account, w.linked_account_id)
        ev = w.link_evidence or {}
        out.append({
            "wallet": w.address,
            "handle": acc.handle if acc else None,
            "confidence": w.link_confidence,
            "token_count": ev.get("token_count"),
            "median_lead_sec": ev.get("median_lead_sec"),
            "is_bot": w.is_bot,
        })
    return out
