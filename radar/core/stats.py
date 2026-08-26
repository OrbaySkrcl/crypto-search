"""Kucuk istatistik yardimcilari.

Tek bir fikir tasirlar: KUCUK ORNEKLEM YALAN SOYLER. 3 atisin 3'unu
tutturan cuzdan %100 isabetli degildir; alt sinir 0.31'dir.
"""
from __future__ import annotations

import math


def wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float:
    """%95 guven araliginin ALT siniri.

    Ham isabet orani kucuk orneklemde yalan soyler:

        3/3   ham %100  -> 0.44   (kanit zayif, sansli olabilir)
        8/8   ham %100  -> 0.68
        30/40 ham  %75  -> 0.60   (kanit guclu)

    Yani "3 atisla %100" tutturan, "40 atista %75" tutturandan DAHA DUSUK
    puan alir. "Az ama oz sniper" boyle odullendirilir, "sansli" degil.
    """
    if total <= 0:
        return 0.0
    p = successes / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return max(0.0, (centre - margin) / denom)


def median(values: list[float]) -> float | None:
    """Ortalama degil medyan: tek bir 100x butun tabloyu bozmasin."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def percentile(values: list[float], q: float) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * max(0.0, min(1.0, q))
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return vals[int(k)]
    return vals[lo] * (hi - k) + vals[hi] * (k - lo)


def log_scale(value: float | None, low: float, high: float) -> float:
    """[low, high] araligini logaritmik olarak 0..1'e sikistirir."""
    if value is None or value <= 0 or high <= low <= 0:
        return 0.0
    v = max(low, min(high, value))
    return (math.log(v) - math.log(low)) / (math.log(high) - math.log(low))


def inverse_log_scale(value: float | None, low: float, high: float) -> float:
    """Kucuk deger = iyi (ornegin giris piyasa degeri)."""
    if value is None or value <= 0:
        return 0.0
    return 1.0 - log_scale(value, low, high)


def tradeable_usd(liquidity_usd: float | None, max_slippage: float = 0.05) -> float:
    """Bu likiditede ~%5 kaymayla pratikte ne kadar dolar girilebilir.

    Sabit carpimli havuzda kayma yaklasik olarak (x/L) kadardir; yani
    L * slippage kadar dolar makul ust siniri verir. $2.000 likiditede
    gorunen 50x, ~$100'luk bir pozisyonun katidir — kagit uzerindedir.
    """
    if not liquidity_usd or liquidity_usd <= 0:
        return 0.0
    return liquidity_usd * max_slippage
