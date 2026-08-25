import math
from datetime import datetime, timedelta, timezone

import pytest

from alpha_hunter.scoring import metrics as M


# --------------------------------------------------------------- guvenilirlik
def test_wilson_penalises_small_samples():
    """3/3 mukemmel gorunur ama kanit zayiftir; 30/40 daha guvenilirdir."""
    lucky = M.wilson_lower_bound(3, 3)
    solid = M.wilson_lower_bound(30, 40)
    assert lucky < solid
    assert 0.2 < lucky < 0.45


def test_wilson_bounds_and_monotonic():
    assert M.wilson_lower_bound(0, 0) == 0.0
    assert M.wilson_lower_bound(0, 50) == 0.0
    assert M.wilson_lower_bound(100, 100) > 0.95
    assert M.wilson_lower_bound(20, 40) > M.wilson_lower_bound(10, 40)


def test_wilson_accepts_fractional_weights():
    """Zaman agirlikli etkin sayimlar kesirli olur."""
    v = M.wilson_lower_bound(3.7, 5.2)
    assert 0.0 <= v <= 1.0


# ------------------------------------------------------------- KURAL 1 / erken
def test_mc_earliness_decreases_with_market_cap():
    low = M.mc_earliness(20_000, 15_000, 10_000_000)
    mid = M.mc_earliness(300_000, 15_000, 10_000_000)
    high = M.mc_earliness(8_000_000, 15_000, 10_000_000)
    assert low > mid > high
    assert M.mc_earliness(5_000, 15_000, 10_000_000) == 1.0
    assert M.mc_earliness(50_000_000, 15_000, 10_000_000) == 0.0
    assert M.mc_earliness(None, 15_000, 10_000_000) == 0.0


def test_run_capture_separates_early_caller_from_copycat():
    """Coin 1k -> 1M gitti. Erken giren yuksek, tepede giren dusuk almalidir."""
    early = M.run_capture(entry_mc=2_000, max_mc_after=1_000_000, baseline_mc=1_000, global_ath_mc=1_000_000)
    late = M.run_capture(entry_mc=500_000, max_mc_after=1_000_000, baseline_mc=1_000, global_ath_mc=1_000_000)
    top = M.run_capture(entry_mc=1_000_000, max_mc_after=1_000_000, baseline_mc=1_000, global_ath_mc=1_000_000)
    assert early > 0.85
    assert late < 0.20
    assert top == pytest.approx(0.0, abs=1e-6)
    assert early > late > top


def test_run_capture_handles_missing_data():
    assert M.run_capture(None, 100, 1, 100) is None
    assert M.run_capture(10, None, 1, 100) is None
    assert M.run_capture(10, 100, None, 100) is None
    assert M.run_capture(10, 100, 100, 100) is None   # baseline == ath


def test_entry_quality_falls_back_without_capture():
    assert M.entry_quality(0.8, None) == pytest.approx(0.8)
    both = M.entry_quality(0.8, 0.2)
    assert 0.2 < both < 0.8


# ------------------------------------------------------------ KURAL 2 / spray
def test_spray_penalty_shape():
    assert M.spray_penalty(1.0, 3.0, 25.0, 0.25) == 1.0
    assert M.spray_penalty(3.0, 3.0, 25.0, 0.25) == 1.0
    assert M.spray_penalty(30.0, 3.0, 25.0, 0.25) == 0.25
    mid = M.spray_penalty(10.0, 3.0, 25.0, 0.25)
    assert 0.25 < mid < 1.0


def test_spray_penalty_monotonic():
    vals = [M.spray_penalty(x, 3.0, 25.0, 0.25) for x in (2, 5, 10, 20, 40)]
    assert vals == sorted(vals, reverse=True)


# --------------------------------------------------------------- echo/ozgunluk
def test_originality_decays_with_delay():
    tau = 21_600.0
    assert M.originality(0, tau) == 1.0
    assert M.originality(None, tau) == 1.0
    assert M.originality(tau, tau) == pytest.approx(math.exp(-1), abs=1e-6)
    assert M.originality(86_400, tau) < 0.05


# ------------------------------------------------------------------- buyukluk
def test_magnitude_uses_median_not_mean():
    """Tek bir 100x, 9 tane cop cagriyi kurtaramaz."""
    junk = [0.2] * 9 + [100.0]
    good = [4.0] * 10
    w = [1.0] * 10
    assert M.magnitude_score(good, w, 10.0) > M.magnitude_score(junk, w, 10.0)
    assert M.magnitude_score(junk, w, 10.0) == 0.0


def test_magnitude_caps_at_target():
    assert M.magnitude_score([1000.0] * 5, [1.0] * 5, 10.0) == 1.0
    assert M.magnitude_score([], [], 10.0) == 0.0


def test_weighted_median_respects_weights():
    assert M.weighted_median([1.0, 10.0], [0.01, 100.0]) == 10.0
    assert M.weighted_median([1.0, 10.0], [100.0, 0.01]) == 1.0


# ------------------------------------------------------------------- zaman
def test_recency_weight_halves_at_halflife():
    now = datetime(2025, 6, 1, tzinfo=timezone.utc)
    assert M.recency_weight(now, now, 45) == 1.0
    assert M.recency_weight(now - timedelta(days=45), now, 45) == pytest.approx(0.5)
    assert M.recency_weight(now - timedelta(days=90), now, 45) == pytest.approx(0.25)


def test_consistency_prefers_spread_out_wins():
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    calls = [base + timedelta(days=7 * i) for i in range(8)]
    spread_wins = [base + timedelta(days=7 * i) for i in (0, 2, 4, 6)]
    clustered = [base, base + timedelta(days=1), base + timedelta(days=2)]
    assert M.consistency(calls, spread_wins) > M.consistency(calls, clustered)
    assert M.consistency(calls, []) == 0.0


# ---------------------------------------------------------- gercekci tepe
def test_sustained_high_ignores_one_candle_spikes():
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    # 1 dakikalik 100'luk fitil, ardindan 15+ dakika 10 seviyesi
    series = [(base, 10.0), (base + timedelta(minutes=1), 100.0)]
    series += [(base + timedelta(minutes=2 + i), 10.0) for i in range(20)]
    got = M.sustained_high(series, 15)
    assert got == 10.0


def test_sustained_high_accepts_real_plateau():
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    series = [(base + timedelta(minutes=i), 50.0) for i in range(30)]
    assert M.sustained_high(series, 15) == 50.0
    assert M.sustained_high([], 15) is None


# ------------------------------------------------------------------ bilesik
def test_composite_alpha_ranges_and_ordering():
    w = {"reliability": 0.32, "magnitude": 0.24, "entry_quality": 0.20,
         "survivorship": 0.12, "originality": 0.12}
    perfect = M.composite_alpha(
        reliability=1, magnitude=1, entry_q=1, survivorship=1, original=1,
        weights=w, spray=1.0, consist=1.0, data_conf=1.0,
    )
    sprayer = M.composite_alpha(
        reliability=1, magnitude=1, entry_q=1, survivorship=1, original=1,
        weights=w, spray=0.25, consist=1.0, data_conf=1.0,
    )
    empty = M.composite_alpha(
        reliability=0, magnitude=0, entry_q=0, survivorship=0, original=0,
        weights=w, spray=1.0, consist=0.0, data_conf=0.0,
    )
    assert perfect == pytest.approx(100.0)
    assert sprayer == pytest.approx(25.0)
    assert empty == 0.0


def test_tier_gate_requires_minimum_sample():
    assert M.tier_for(95.0, n_evaluated=2, min_calls=4) == "UNRATED"
    assert M.tier_for(95.0, n_evaluated=10, min_calls=4) == "S"
    assert M.tier_for(10.0, n_evaluated=10, min_calls=4) == "F"


def test_jaccard():
    assert M.jaccard({1, 2, 3}, {1, 2, 3}) == 1.0
    assert M.jaccard({1, 2}, {3, 4}) == 0.0
    assert M.jaccard(set(), {1}) == 0.0
