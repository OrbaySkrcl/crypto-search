"""Saf matematik. Yan etkisiz, birim testi yapilabilir.

Buradaki her fonksiyon "gurultuyu eleyen" bir istatistik parcasidir.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------- #
#  1) Guvenilirlik: Wilson alt sinirn
# --------------------------------------------------------------------------- #
def wilson_lower_bound(successes: float, total: float, z: float = 1.96) -> float:
    """Basari oraninin %95 guven araliginin ALT siniri.

    Neden ham win-rate degil: 3/3 tutturan hesap %100 gorunur ama kanit zayiftir.
    Wilson bunu ~0.31'e ceker; 30/40 tutturan hesap ~0.60 alir. Yani "az ama oz"
    dogru sekilde odullendirilir, "3 atisla sansli" odullendirilmez.

    Kesirli (agirlikli) sayilarla da calisir -- zaman agirlikli etkin sayimlar icin.
    """
    if total <= 0:
        return 0.0
    p = successes / total
    z2 = z * z
    denom = 1.0 + z2 / total
    centre = p + z2 / (2.0 * total)
    margin = z * math.sqrt(max(0.0, (p * (1.0 - p) + z2 / (4.0 * total)) / total))
    return clamp((centre - margin) / denom)


# --------------------------------------------------------------------------- #
#  2) Zaman agirligi
# --------------------------------------------------------------------------- #
def recency_weight(called_at: datetime, now: datetime, halflife_days: float) -> float:
    """6 ay onceki basari, dunku basari kadar deger etmez."""
    if halflife_days <= 0:
        return 1.0
    age_days = max(0.0, (now - called_at).total_seconds() / 86400.0)
    return 0.5 ** (age_days / halflife_days)


# --------------------------------------------------------------------------- #
#  3) KURAL 1 -- Zaman damgasi / erkencilik
# --------------------------------------------------------------------------- #
def mc_earliness(entry_mc: float | None, low: float, high: float) -> float:
    """Mutlak piyasa degeri seviyesine gore erkencilik (log olcek).

    30k MC'de cagri ~1.0, 8M MC'de cagri ~0.0. Cunku 8M'den 10x cok daha zordur.
    """
    if not entry_mc or entry_mc <= 0:
        return 0.0
    if entry_mc <= low:
        return 1.0
    if entry_mc >= high:
        return 0.0
    return clamp((math.log10(high) - math.log10(entry_mc)) / (math.log10(high) - math.log10(low)))


def run_capture(
    entry_mc: float | None,
    max_mc_after: float | None,
    baseline_mc: float | None,
    global_ath_mc: float | None,
) -> float | None:
    """COPYCAT DEDEKTORU.

    Coin'in toplam log-yukselisinin yuzde kacini bu cagri yakaladi?

        capture = log(max_after / entry) / log(global_ath / baseline)

    Ornek: coin 1k -> 1M gitti (3 kat buyukluk).
      * 2k'da cagiran:   log(500)/log(1000)  = 0.90  -> gercek erken cagri
      * 500k'da cagiran: log(2)/log(1000)    = 0.10  -> copycat, zirveye yakin girdi
      * 1M'de cagiran:   0.0                        -> tam tepe, eksi puan

    `baseline_mc` genelde token'in dogum/ilk gorulme MC'sidir.
    """
    if not entry_mc or entry_mc <= 0 or not max_mc_after or max_mc_after <= 0:
        return None
    if not global_ath_mc or not baseline_mc or baseline_mc <= 0 or global_ath_mc <= baseline_mc:
        return None
    total_run = math.log(global_ath_mc / baseline_mc)
    if total_run <= 1e-9:
        return None
    captured = math.log(max(max_mc_after, entry_mc) / entry_mc)
    return clamp(captured / total_run)


def entry_quality(mc_early: float | None, capture: float | None) -> float:
    """Erkenciligin bilesigi. run_capture yoksa yalnizca MC seviyesine duser."""
    if capture is None:
        return clamp(mc_early or 0.0)
    return clamp(0.45 * (mc_early or 0.0) + 0.55 * capture)


# --------------------------------------------------------------------------- #
#  4) KURAL 2 -- Spray & Pray filtresi
# --------------------------------------------------------------------------- #
def spray_penalty(calls_per_day: float, soft: float, hard: float, floor: float) -> float:
    """Gunde 30 CA atan hesap insider degil kumarbazdir.

    <= soft (varsayilan 3/gun) ceza yok; sonra 1/x ile duser, hard esiginde tabana oturur.
    """
    if calls_per_day <= soft:
        return 1.0
    if calls_per_day >= hard:
        return floor
    ratio = soft / calls_per_day
    return clamp(floor + (ratio - floor) * 1.0, floor, 1.0)


# --------------------------------------------------------------------------- #
#  5) Echo / ozgunluk
# --------------------------------------------------------------------------- #
def originality(echo_delay_sec: int | None, tau_sec: float) -> float:
    """Ilk cagiran 1.0 alir; 6 saat sonra ayni CA'yi atan ~0.37, 1 gun sonra ~0.02.

    Uyari: bu metrik bizim kapsamimiza baglidir -- sadece taradigimiz tweetleri
    bilebiliriz. Kapsam genisledikce dogrulugu artar.
    """
    if echo_delay_sec is None:
        return 1.0
    if echo_delay_sec <= 0:
        return 1.0
    return clamp(math.exp(-echo_delay_sec / max(1.0, tau_sec)))


# --------------------------------------------------------------------------- #
#  6) Buyukluk (magnitude)
# --------------------------------------------------------------------------- #
def weighted_median(values: Sequence[float], weights: Sequence[float]) -> float:
    if not values:
        return 0.0
    pairs = sorted(zip(values, weights, strict=True), key=lambda p: p[0])
    total = sum(w for _, w in pairs)
    if total <= 0:
        return pairs[len(pairs) // 2][0]
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2.0:
            return v
    return pairs[-1][0]


def weighted_quantile(values: Sequence[float], weights: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    pairs = sorted(zip(values, weights, strict=True), key=lambda p: p[0])
    total = sum(w for _, w in pairs)
    if total <= 0:
        return pairs[-1][0]
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total * q:
            return v
    return pairs[-1][0]


def magnitude_score(multiples: Sequence[float], weights: Sequence[float], target: float) -> float:
    """Kazanclarin buyuklugu -- MEDYAN uzerinden (ortalama degil).

    Tek bir 100x'in, 40 tane cop cagriyi kurtarmasini engeller.
    """
    if not multiples or target <= 1:
        return 0.0
    med = weighted_median([max(m, 1e-6) for m in multiples], list(weights))
    if med <= 1.0:
        return 0.0
    return clamp(math.log10(med) / math.log10(target))


# --------------------------------------------------------------------------- #
#  7) Tutarlilik
# --------------------------------------------------------------------------- #
def consistency(call_times: Iterable[datetime], win_times: Iterable[datetime]) -> float:
    """Kazanclar tek bir haftaya sikismis mi, zamana yayilmis mi?

    Tek hafta patlamasi = sansli donem veya tek bir pump grubuna dahil olmak.
    """
    call_weeks = {_week_key(t) for t in call_times}
    win_weeks = {_week_key(t) for t in win_times}
    if not call_weeks:
        return 0.0
    if not win_weeks:
        return 0.0
    spread = len(win_weeks) / len(call_weeks)
    # Birden fazla aktif haftada kazanmis olmak asil sinyal
    depth = clamp(len(win_weeks) / 4.0)
    return clamp(0.5 * spread + 0.5 * depth)


def _week_key(t: datetime) -> tuple[int, int]:
    iso = t.isocalendar()
    return (iso[0], iso[1])


# --------------------------------------------------------------------------- #
#  8) Bilesik alfa skoru
# --------------------------------------------------------------------------- #
def composite_alpha(
    *,
    reliability: float,
    magnitude: float,
    entry_q: float,
    survivorship: float,
    original: float,
    weights: dict[str, float],
    spray: float,
    consist: float,
    data_conf: float,
    market_edge: float = 0.0,
    tradeable: float = 1.0,
) -> float:
    raw = (
        weights["reliability"] * clamp(reliability)
        + weights["magnitude"] * clamp(magnitude)
        + weights.get("market_edge", 0.0) * clamp(market_edge)
        + weights["entry_quality"] * clamp(entry_q)
        + weights["survivorship"] * clamp(survivorship)
        + weights["originality"] * clamp(original)
    )
    # Carpanlarin hepsinin tabani var: cezalandirir ama sifirlamaz.
    consist_factor = 0.75 + 0.25 * clamp(consist)
    conf_factor = 0.60 + 0.40 * clamp(data_conf)
    # Yalnizca girilemeyecek kadar ince tokenlar cagiran hesap, kagit uzerinde
    # ne kadar iyi gorunurse gorunsun kullanilabilir degildir.
    trade_factor = 0.55 + 0.45 * clamp(tradeable)
    return clamp(raw * spray * consist_factor * conf_factor * trade_factor) * 100.0


def tier_for(score: float, n_evaluated: int, min_calls: int) -> str:
    if n_evaluated < min_calls:
        return "UNRATED"
    if score >= 80:
        return "S"
    if score >= 65:
        return "A"
    if score >= 50:
        return "B"
    if score >= 35:
        return "C"
    if score >= 20:
        return "D"
    return "F"


# --------------------------------------------------------------------------- #
#  9) Koordinasyon tespiti
# --------------------------------------------------------------------------- #
def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def sustained_high(
    candles: Sequence[tuple[datetime, float]], min_minutes: int
) -> float | None:
    """ATH kagit uzerindedir. 30 saniye goren fiyattan cikamazsin.

    En az `min_minutes` boyunca UZERINDE kalinan en yuksek seviyeyi dondurur --
    yani gercekten satilabilir tepe.
    """
    if not candles:
        return None
    pts = sorted(candles, key=lambda c: c[0])
    levels = sorted({p[1] for p in pts}, reverse=True)
    need = timedelta(minutes=min_minutes)
    for level in levels:
        run_start: datetime | None = None
        for ts, val in pts:
            if val >= level:
                if run_start is None:
                    run_start = ts
                elif ts - run_start >= need:
                    return level
            else:
                run_start = None
        # Tek mumluk pencerelerde sure birikmez; mum genisligini de say
        if len(pts) >= 2:
            step = (pts[1][0] - pts[0][0])
            count = sum(1 for _, v in pts if v >= level)
            if step * count >= need:
                return level
    return pts[0][1] if pts else None
