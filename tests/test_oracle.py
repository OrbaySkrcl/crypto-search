"""Oracle katmani testleri -- gercek API govde sekilleriyle, agsiz.

Senaryo: token 10k MC'de dogar, 40k'da tweetlenir, 3 saat sonra 2M yapar.
Ayni tokene 1.5M'de giren bir copycat da var.
"""
from datetime import datetime, timedelta, timezone

import pytest

from alpha_hunter.http import HttpClient
from alpha_hunter.oracle.dexscreener import DexScreenerClient
from alpha_hunter.oracle.geckoterminal import RESOLUTIONS
from alpha_hunter.oracle.resolver import PriceOracle

MINT = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
PAIR = "8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj"
SUPPLY = 1_000_000_000

BIRTH = datetime(2025, 3, 1, 0, 0, tzinfo=timezone.utc)
TWEET = BIRTH + timedelta(hours=2)          # 40k MC
PEAK = BIRTH + timedelta(hours=5)           # 2M MC
NOW = BIRTH + timedelta(hours=30)


def _price_at(minute: int) -> float:
    """Dakikaya gore fiyat egrisi. MC = fiyat * 1e9."""
    t = BIRTH + timedelta(minutes=minute)
    if t < TWEET:                                    # 10k -> 40k
        frac = (t - BIRTH).total_seconds() / (TWEET - BIRTH).total_seconds()
        mc = 10_000 + frac * 30_000
    elif t <= PEAK:                                  # 40k -> 2M
        frac = (t - TWEET).total_seconds() / (PEAK - TWEET).total_seconds()
        mc = 40_000 * ((2_000_000 / 40_000) ** frac)
    else:                                            # 2M -> 400k dususu
        frac = min(1.0, (t - PEAK).total_seconds() / (6 * 3600))
        mc = 2_000_000 * ((400_000 / 2_000_000) ** frac)
    return mc / SUPPLY


TOTAL_MINUTES = int((NOW - BIRTH).total_seconds() // 60)
SERIES = [(BIRTH + timedelta(minutes=i), _price_at(i)) for i in range(TOTAL_MINUTES)]

DEX_PAYLOAD = {
    "schemaVersion": "1.0.0",
    "pairs": [
        {
            "chainId": "solana",
            "dexId": "raydium",
            "pairAddress": PAIR,
            "baseToken": {"address": MINT, "name": "Test Coin", "symbol": "TEST"},
            "quoteToken": {"address": "So11111111111111111111111111111111111111112", "symbol": "SOL"},
            "priceNative": "0.0000004",
            "priceUsd": f"{SERIES[-1][1]:.12f}",
            "txns": {"h24": {"buys": 4200, "sells": 3100}},
            "volume": {"h24": 5_400_000},
            "priceChange": {"h24": 380},
            "liquidity": {"usd": 185_000, "base": 1, "quote": 2},
            "fdv": SERIES[-1][1] * SUPPLY,
            "marketCap": SERIES[-1][1] * SUPPLY,
            "pairCreatedAt": int(BIRTH.timestamp() * 1000),
        },
        {   # ayni token, dusuk likiditeli ikinci havuz -> secilmemeli
            "chainId": "solana", "dexId": "orca", "pairAddress": "LOWLIQ",
            "baseToken": {"address": MINT, "symbol": "TEST"},
            "quoteToken": {"symbol": "USDC"},
            "priceUsd": "0.00001", "liquidity": {"usd": 900},
            "fdv": 10_000, "marketCap": 10_000,
            "pairCreatedAt": int(BIRTH.timestamp() * 1000),
        },
    ],
}


def _aggregate(resolution: str, before_ts: int | None, limit: int):
    """SERIES'i istenen cozunurluge topla, GeckoTerminal formatinda dondur."""
    _, _, secs = RESOLUTIONS[resolution]
    buckets: dict[int, list[float]] = {}
    for ts, price in SERIES:
        key = int(ts.timestamp()) // secs * secs
        buckets.setdefault(key, []).append(price)
    rows = []
    for key in sorted(buckets):
        if before_ts is not None and key >= before_ts:
            continue
        vals = buckets[key]
        rows.append([key, vals[0], max(vals), min(vals), vals[-1], 1000.0])
    rows = rows[-limit:]
    rows.reverse()                        # API azalan sirada doner
    return {"data": {"attributes": {"ohlcv_list": rows}}}


class FakeHttp(HttpClient):
    """Agi taklit eder; cagrilari sayar."""

    def __init__(self):
        super().__init__()
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get(self, url, *, bucket="default", params=None, headers=None,
                  expect_json=True, max_retries=None):
        self.calls.append(url)
        if "/latest/dex/tokens/" in url or "/latest/dex/pairs/" in url:
            return DEX_PAYLOAD
        if "/ohlcv/" in url:
            resolution = next(
                (r for r, (tf, agg, _) in RESOLUTIONS.items()
                 if url.endswith("/" + tf) and str(agg) == str((params or {}).get("aggregate"))),
                "1m",
            )
            before = (params or {}).get("before_timestamp")
            return _aggregate(resolution, int(before) if before else None,
                              int((params or {}).get("limit", 1000)))
        if "/tokens/" in url and url.endswith("/pools"):
            return {"data": [{"attributes": {"address": PAIR, "reserve_in_usd": "185000"}}]}
        return None

    async def post(self, url, *, bucket="default", json_body=None, headers=None, max_retries=None):
        self.calls.append(url)
        return None


@pytest.fixture()
def http():
    return FakeHttp()


# --------------------------------------------------------------- dexscreener
async def test_dexscreener_picks_highest_liquidity_pair(http):
    info = await DexScreenerClient(http).token(MINT, "solana")
    assert info is not None
    assert info.pair_address == PAIR            # LOWLIQ degil
    assert info.symbol == "TEST"
    assert info.chain == "solana"
    assert info.liquidity_usd == 185_000
    assert info.pair_created_at == BIRTH


async def test_supply_estimate_derived_from_marketcap_and_price(http):
    info = await DexScreenerClient(http).token(MINT, "solana")
    assert info.supply_estimate == pytest.approx(SUPPLY, rel=1e-6)
    assert info.mc_from_price(0.001) == pytest.approx(1_000_000, rel=1e-6)


async def test_batching_respects_thirty_address_limit(http):
    addrs = [MINT] + [f"{i:044d}" for i in range(45)]
    await DexScreenerClient(http).tokens(addrs)
    token_calls = [c for c in http.calls if "/latest/dex/tokens/" in c]
    assert len(token_calls) == 2                # 46 adres -> 2 istek
    assert all(c.split("/")[-1].count(",") < 30 for c in token_calls)


# ------------------------------------------------------------------- resolver
async def test_evaluate_early_call_captures_the_run(http, monkeypatch):
    monkeypatch.setattr(
        "alpha_hunter.oracle.resolver.datetime", _FrozenDatetime, raising=False
    )
    oracle = PriceOracle(http)
    ev = await oracle.evaluate("solana", MINT, TWEET)

    assert ev.ok, ev.reason
    # 40k MC'de girdi
    assert ev.entry_mc_usd == pytest.approx(40_000, rel=0.05)
    assert ev.entry_confidence >= 0.9
    # 2M tepeye 50x
    assert ev.max_multiple == pytest.approx(50, rel=0.10)
    assert ev.max_mc_usd_after == pytest.approx(2_000_000, rel=0.05)
    # tweetten onceki tepe 40k'yi asmamali (copycat degil)
    assert ev.pre_ath_mc_usd <= 45_000
    # kosunun neredeyse tamamini yakaladi
    assert ev.run_capture > 0.70
    assert ev.mc_earliness > 0.55
    assert ev.entry_quality > 0.6
    # tepe 15 dakikadan uzun korunmadigi icin gercekci kat daha dusuk olabilir
    assert 1.0 < ev.sustained_multiple <= ev.max_multiple * 1.01


async def test_evaluate_copycat_gets_low_run_capture(http, monkeypatch):
    monkeypatch.setattr(
        "alpha_hunter.oracle.resolver.datetime", _FrozenDatetime, raising=False
    )
    oracle = PriceOracle(http)
    late = PEAK - timedelta(minutes=20)          # zirveye cok yakin
    ev = await oracle.evaluate("solana", MINT, late)

    assert ev.ok
    assert ev.entry_mc_usd > 1_000_000
    assert ev.max_multiple < 2.0
    assert ev.run_capture < 0.20                 # kosunun ancak kirintisi
    assert ev.mc_earliness < 0.35
    assert ev.entry_quality < 0.35


async def test_evaluate_windows_and_drawdown(http, monkeypatch):
    monkeypatch.setattr(
        "alpha_hunter.oracle.resolver.datetime", _FrozenDatetime, raising=False
    )
    ev = await PriceOracle(http).evaluate("solana", MINT, TWEET)
    assert "1" in ev.multiple_by_window            # T+1s
    assert "6" in ev.multiple_by_window            # T+6s
    assert ev.multiple_by_window["6"] > 1.0
    # 2M -> 400k dususu ~%80
    assert ev.max_drawdown_after == pytest.approx(0.8, abs=0.08)
    # zaman dondurulmus olmali: 7 gunluk pencere henuz kapanmadi
    assert ev.window_closed is False
    assert ev.history_points > 100


async def test_evaluate_unknown_token_fails_cleanly():
    class Empty(FakeHttp):
        async def get(self, url, **kw):
            return None

    ev = await PriceOracle(Empty()).evaluate("solana", MINT, TWEET)
    assert not ev.ok
    assert "bulunamadi" in (ev.reason or "")


class _FrozenDatetime(datetime):
    """`now()` cagrilarini senaryonun sonuna sabitler."""

    @classmethod
    def now(cls, tz=None):
        return NOW
