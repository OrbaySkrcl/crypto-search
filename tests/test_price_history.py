from datetime import datetime, timedelta, timezone

import pytest

from alpha_hunter.oracle.types import Candle, PriceHistory, TokenInfo

BASE = datetime(2025, 3, 1, 12, 0, tzinfo=timezone.utc)


def mk(n=60, start=BASE, price=1.0, step=60):
    return [
        Candle(start + timedelta(seconds=step * i), price, price * 1.1, price * 0.9, price, 100.0, step)
        for i in range(n)
    ]


def test_price_at_inside_candle_is_high_confidence():
    h = PriceHistory("solana", "x")
    h.merge([Candle(BASE, 1.0, 2.0, 0.5, 3.0, 0, 60)], "test")
    price, conf = h.price_at(BASE + timedelta(seconds=30))
    assert conf == 1.0
    assert price == pytest.approx(2.0)   # acilis 1 -> kapanis 3, ortada 2


def test_price_at_after_series_degrades_confidence():
    h = PriceHistory("solana", "x")
    h.merge([Candle(BASE, 1.0, 1.0, 1.0, 1.0, 0, 60)], "test")
    near = h.price_at(BASE + timedelta(minutes=10))
    far = h.price_at(BASE + timedelta(minutes=50))
    assert near[1] > far[1]
    assert h.price_at(BASE + timedelta(hours=5)) is None


def test_price_at_returns_none_before_series():
    h = PriceHistory("solana", "x")
    h.merge(mk(10), "test")
    assert h.price_at(BASE - timedelta(hours=3)) is None


def test_finer_resolution_wins_on_merge():
    h = PriceHistory("solana", "x")
    h.merge([Candle(BASE, 1, 1, 1, 1, 0, 3600)], "coarse")
    h.merge([Candle(BASE, 2, 2, 2, 2, 0, 60)], "fine")
    assert h.candle_at(BASE).seconds == 60
    assert h.candle_at(BASE).close == 2
    assert h.sources == {"coarse", "fine"}


def test_max_high_respects_window():
    h = PriceHistory("solana", "x")
    candles = [
        Candle(BASE, 1, 1, 1, 1, 0, 60),
        Candle(BASE + timedelta(minutes=1), 1, 99, 1, 1, 0, 60),
        Candle(BASE + timedelta(minutes=2), 1, 5, 1, 1, 0, 60),
    ]
    h.merge(candles, "t")
    assert h.max_high(BASE, BASE + timedelta(minutes=3))[0] == 99
    # 99'lu mumu disarida birak
    assert h.max_high(BASE + timedelta(minutes=2), BASE + timedelta(minutes=3))[0] == 5


def test_max_drawdown():
    h = PriceHistory("solana", "x")
    h.merge(
        [
            Candle(BASE, 10, 10, 10, 10, 0, 60),
            Candle(BASE + timedelta(minutes=1), 10, 10, 2, 2, 0, 60),
        ],
        "t",
    )
    dd = h.max_drawdown(BASE, BASE + timedelta(minutes=5))
    assert dd == pytest.approx(0.8)


def test_token_info_mc_from_price():
    info = TokenInfo(chain="solana", address="x", supply_estimate=1_000_000_000)
    assert info.mc_from_price(0.001) == pytest.approx(1_000_000)
    assert TokenInfo(chain="solana", address="x").mc_from_price(1.0) is None
