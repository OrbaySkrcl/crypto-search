"""ADIM 6 — KARNE: botun kendi sicilini tutmasi.

Bu modul urunun en onemli parcasi. Bir sinyal botunun degeri "kac kart
gonderdigi" degil, "gonderdigi kartlarin ne yaptigi"dir. Her alarm
kaydedilir ve +1s / +6s / +24s'te OLCULUR.

Sonuc kotuyse bunu gizlemiyoruz: /karne kotu sayilari da gosterir. Bir
sinyal tipi para kaybettiriyorsa kullanicinin bunu bilmesi gerekir —
aksi halde bot "tatmin eden" bir oyuncaktir.

Olcum kendi fiyat orneklerimizden yapilir; disariya bagimlilik yoktur.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import Alert, Outcome, session_scope, utcnow
from . import repo
from .stats import median

log = logging.getLogger(__name__)


def evaluate_alerts(session: Session, limit: int = 500) -> dict:
    """Acik alarmlari olcer; suresi dolanlari kapatir."""
    horizon_h = max(settings.journal_hour_list or [24])
    rows = list(
        session.scalars(
            select(Alert)
            .where(Alert.outcome == Outcome.OPEN.value)
            .order_by(Alert.created_at.asc())
            .limit(limit)
        )
    )
    stats = {"olculen": 0, "kapanan": 0}
    now = utcnow()

    for al in rows:
        entry = al.entry_mc_usd
        if not entry or entry <= 0:
            al.outcome = Outcome.FLAT.value
            al.closed_at = now
            stats["kapanan"] += 1
            continue

        age_h = (now - al.created_at).total_seconds() / 3600

        for h, field in ((1, "mult_1h"), (6, "mult_6h"), (24, "mult_24h")):
            if h not in settings.journal_hour_list:
                continue
            if getattr(al, field) is not None or age_h < h:
                continue
            mc = repo.price_at(session, al.token_id, al.created_at + timedelta(hours=h))
            if mc:
                setattr(al, field, round(mc / entry, 4))
                stats["olculen"] += 1

        peak, _at = repo.sustained_peak(
            session,
            al.token_id,
            since=al.created_at,
            until=al.created_at + timedelta(hours=horizon_h),
        )
        if peak:
            al.peak_mult = round(peak / entry, 4)

        if age_h >= horizon_h:
            al.outcome = _classify(al).value
            al.closed_at = now
            stats["kapanan"] += 1

    return stats


def _classify(al: Alert) -> Outcome:
    """Kart isabetli miydi?

    ISABET  : korunmus tepe, giris degerinin `journal_win_multiple` katini gecti
              (yani kart geldikten sonra GERCEKTEN cikilabilecek bir kar olustu)
    ZARAR   : 24 saat sonunda giris degerinin altina indi
    NOTR    : ikisi de degil
    """
    if al.peak_mult and al.peak_mult >= settings.journal_win_multiple:
        return Outcome.WIN
    last = al.mult_24h or al.mult_6h or al.mult_1h
    if last is not None and last <= settings.journal_loss_multiple:
        return Outcome.LOSS
    return Outcome.FLAT


# --------------------------------------------------------------------------- #
#  Okuma
# --------------------------------------------------------------------------- #
def scorecard(session: Session, days: int = 30) -> dict:
    """Sinyal tipine gore gercek sicil."""
    since = utcnow() - timedelta(days=days)
    rows = list(session.scalars(select(Alert).where(Alert.created_at >= since)))

    def block(subset: list[Alert]) -> dict:
        closed = [a for a in subset if a.outcome != Outcome.OPEN.value]
        wins = [a for a in closed if a.outcome == Outcome.WIN.value]
        losses = [a for a in closed if a.outcome == Outcome.LOSS.value]
        peaks = [a.peak_mult for a in closed if a.peak_mult]
        d24 = [a.mult_24h for a in closed if a.mult_24h]
        return {
            "toplam": len(subset),
            "acik": len(subset) - len(closed),
            "kapali": len(closed),
            "isabet": len(wins),
            "zarar": len(losses),
            "isabet_orani": (len(wins) / len(closed)) if closed else None,
            "medyan_tepe": median(peaks),
            "medyan_24s": median(d24),
        }

    out = {"gun": days, "genel": block(rows), "tip": {}}
    for kind in sorted({a.kind for a in rows}):
        out["tip"][kind] = block([a for a in rows if a.kind == kind])
    return out


def kind_stats(session: Session, kind: str, days: int = 30) -> dict | None:
    """Tek bir sinyal tipinin sicili — kartin altina basmak icin."""
    card = scorecard(session, days=days)
    data = card["tip"].get(kind)
    if not data or not data["kapali"]:
        return None
    return data


def run_journal() -> dict:
    with session_scope() as s:
        return evaluate_alerts(s)
