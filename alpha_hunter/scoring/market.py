"""Piyasa cipasi ve alinabilirlik.

Iki soru, ikisi de puanin dogrulugu icin sart:

1. "5x yapti" ne kadar iyi?  Herkesin 6x yaptigi bir gunde 5x, beceri degil
   piyasada bulunmaktir. Cagriyi AYNI ZAMAN DILIMINDE baskalarinin cagirdigi
   tokenlarin medyaniyla karsilastiriyoruz.

2. O 5x'e girilebilir miydi?  $2.000 likiditesi olan bir havuzda 50x gorunur
   ama $500'luk alim fiyati %30 kaydirir. Kagit uzerindeki kat degil,
   makul kaymayla girilebilecek BUYUKLUK onemli.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db.models import Call, CallOutcome
from .metrics import clamp

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  1) Kohort medyani  --  "o gun herkes ne yapti?"
# --------------------------------------------------------------------------- #
def cohort_median_multiple(
    session: Session,
    chain: str,
    at: datetime,
    exclude_account_id: int | None = None,
    exclude_call_id: int | None = None,
) -> tuple[float | None, int]:
    """Ayni zaman diliminde BASKALARININ cagirdigi tokenlarin medyan kati.

    Pencere yeterli ornek bulana kadar genisler. Hicbir zaman hesabin kendi
    cagrilariyla karsilastirma yapilmaz -- kendi ortalamasini gecmek beceri
    degildir.

    (medyan, kohort_buyuklugu) doner. Yeterli ornek yoksa (None, n).
    """
    window = settings.cohort_window_hours
    mults: list[float] = []
    while window <= settings.cohort_max_window_hours:
        stmt = select(Call).where(
            Call.chain == chain,
            Call.called_at >= at - timedelta(hours=window),
            Call.called_at <= at + timedelta(hours=window),
            Call.outcome.in_([CallOutcome.WIN, CallOutcome.LOSS, CallOutcome.RUG]),
        )
        if exclude_account_id is not None:
            stmt = stmt.where(Call.account_id != exclude_account_id)
        if exclude_call_id is not None:
            stmt = stmt.where(Call.id != exclude_call_id)

        mults = [
            c.sustained_multiple or c.max_multiple
            for c in session.scalars(stmt)
            if (c.sustained_multiple or c.max_multiple)
        ]
        if len(mults) >= settings.cohort_min_size:
            mults.sort()
            mid = len(mults) // 2
            median = (
                mults[mid] if len(mults) % 2
                else (mults[mid - 1] + mults[mid]) / 2.0
            )
            return (max(median, 1e-6), len(mults))
        window *= 2

    return (None, len(mults))


def excess_multiple(own: float | None, cohort_median: float | None) -> float | None:
    """Kohortu kac kat gecti. 1.0 = piyasayla ayni, 2.0 = iki kati iyi."""
    if not own or not cohort_median or cohort_median <= 0:
        return None
    return own / cohort_median


def market_edge_score(excesses: list[float], weights: list[float]) -> float:
    """Kohort ustu getirinin puani (0..1).

    Medyan uzerinden ve log olcekte: 1x = piyasayla ayni = 0 puan,
    3x = kohortun uc kati = tam puan.
    """
    if not excesses:
        return 0.0
    from .metrics import weighted_median

    med = weighted_median([max(e, 1e-6) for e in excesses], weights)
    if med <= 1.0:
        return 0.0
    return clamp(math.log(med) / math.log(3.0))


# --------------------------------------------------------------------------- #
#  2) Alinabilirlik  --  "o fiyattan gercekten girebilir miydin?"
# --------------------------------------------------------------------------- #
def tradeable_usd(liquidity_usd: float | None, max_slippage: float | None = None) -> float | None:
    """Kabul edilebilir kaymayla girilebilecek yaklasik dolar buyuklugu.

    Sabit-carpim havuzunda (x*y=k) dolar tarafi rezerv ~ likidite/2'dir.
    R rezervine X dolar sokunca fiyat etkisi ~ X/(R+X), yani hedef kayma s icin:

        X = R * s / (1 - s)

    $100k likidite, %5 kayma -> ~$2.600. Gercekci bir rakam; "50x yapti"
    diyen $2k likiditeli bir coinde ise ~$50 cikar, ki pratikte girilemez.
    """
    if not liquidity_usd or liquidity_usd <= 0:
        return None
    s = max_slippage if max_slippage is not None else settings.max_slippage
    s = min(max(s, 0.001), 0.5)
    reserve = liquidity_usd / 2.0
    return reserve * (s / (1.0 - s))


def tradeability(size_usd: float | None) -> float:
    """0..1 -- $100 girilebiliyorsa 0, $10.000 girilebiliyorsa 1 (log olcek)."""
    lo, hi = settings.tradeable_floor_usd, settings.tradeable_target_usd
    if not size_usd or size_usd <= lo:
        return 0.0
    if size_usd >= hi:
        return 1.0
    return clamp((math.log10(size_usd) - math.log10(lo)) / (math.log10(hi) - math.log10(lo)))


def realizable_profit_usd(size_usd: float | None, multiple: float | None) -> float | None:
    """Maksimum buyuklukle girilseydi elde edilecek kar (kabaca)."""
    if not size_usd or not multiple:
        return None
    return size_usd * (multiple - 1.0)
