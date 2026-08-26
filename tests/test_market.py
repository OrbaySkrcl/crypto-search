"""Piyasa cipasi ve alinabilirlik testleri."""
import itertools
from datetime import datetime, timedelta, timezone

import pytest

from alpha_hunter.config import settings
from alpha_hunter.db.models import Account, Call, CallOutcome, Token, TokenStatus, Tweet
from alpha_hunter.db.session import session_scope
from alpha_hunter.scoring import market as MK

NOW = datetime.now(timezone.utc)


# --------------------------------------------------------------- alinabilirlik
@pytest.mark.parametrize("likidite,beklenen_yaklasik", [
    (100_000, 2_631),     # saglam havuz -> ~2.6k dolar girilebilir
    (10_000, 263),
    (2_000, 52),          # "50x yapti" denen ince havuz -> pratikte girilemez
])
def test_tradeable_size_follows_liquidity(likidite, beklenen_yaklasik):
    got = MK.tradeable_usd(likidite, max_slippage=0.05)
    assert got == pytest.approx(beklenen_yaklasik, rel=0.02)


def test_tradeable_size_needs_liquidity():
    assert MK.tradeable_usd(None) is None
    assert MK.tradeable_usd(0) is None


def test_tradeability_is_zero_for_dust_and_one_for_deep_pools():
    assert MK.tradeability(MK.tradeable_usd(2_000)) < 0.4     # ince havuz
    assert MK.tradeability(MK.tradeable_usd(500_000)) == 1.0  # derin havuz
    assert MK.tradeability(None) == 0.0
    assert MK.tradeability(50) == 0.0                          # tabanin altinda


def test_tradeability_is_monotonic():
    vals = [MK.tradeability(MK.tradeable_usd(x)) for x in (1_000, 10_000, 100_000, 1_000_000)]
    assert vals == sorted(vals)


def test_realizable_profit():
    boyut = MK.tradeable_usd(100_000)
    assert MK.realizable_profit_usd(boyut, 10.0) == pytest.approx(boyut * 9)
    assert MK.realizable_profit_usd(None, 10.0) is None


# ------------------------------------------------------------- kohort cipasi
_SAYAC = itertools.count(1)


def _mk_call(s, acc, when, mult, chain="solana"):
    n = next(_SAYAC)
    tok = Token(chain=chain, address=f"KOH{n:041d}",
                status=TokenStatus.ACTIVE, supply_estimate=1e9)
    s.add(tok)
    s.flush()
    tw = Tweet(account_id=acc.id, platform_tweet_id=f"t{n}", posted_at=when,
               text="CA:", source="test")
    s.add(tw)
    s.flush()
    c = Call(account_id=acc.id, token_id=tok.id, tweet_id=tw.id, chain=chain,
             called_at=when, entry_mc_usd=50_000.0, entry_price_usd=5e-5,
             entry_liquidity_usd=80_000.0, entry_confidence=1.0,
             max_multiple=mult, sustained_multiple=mult,
             outcome=CallOutcome.WIN if mult >= 3 else CallOutcome.LOSS,
             is_closed=True)
    s.add(c)
    s.flush()
    return c


@pytest.fixture()
def kohort(db):
    """Bir gunde 10 hesap ortalama 6x yapiyor; iki hesap ayriksi."""
    with session_scope() as s:
        for i in range(10):
            a = Account(platform="x", handle=f"kalabalik{i}")
            s.add(a)
            s.flush()
            _mk_call(s, a, NOW - timedelta(days=2), 6.0)

        star = Account(platform="x", handle="yildiz")
        s.add(star)
        s.flush()
        star_call = _mk_call(s, star, NOW - timedelta(days=2), 18.0)   # 3x kohort

        lag = Account(platform="x", handle="geride")
        s.add(lag)
        s.flush()
        lag_call = _mk_call(s, lag, NOW - timedelta(days=2), 4.0)      # kohortun altinda
        return {"star": star.id, "star_call": star_call.id,
                "lag": lag.id, "lag_call": lag_call.id}


def test_cohort_median_reflects_what_everyone_else_did(kohort):
    with session_scope() as s:
        median, n = MK.cohort_median_multiple(
            s, "solana", NOW - timedelta(days=2),
            exclude_account_id=kohort["star"], exclude_call_id=kohort["star_call"],
        )
    assert n >= settings.cohort_min_size
    assert median == pytest.approx(6.0, rel=0.35)


def test_own_calls_never_enter_own_cohort(kohort):
    """Kendi ortalamasini gecmek beceri degildir."""
    with session_scope() as s:
        s.add(Account(platform="x", handle="tek"))
        s.flush()
        acc = s.query(Account).filter_by(handle="tek").one()
        for _ in range(9):
            _mk_call(s, acc, NOW - timedelta(days=20), 50.0)
        median, n = MK.cohort_median_multiple(
            s, "solana", NOW - timedelta(days=20), exclude_account_id=acc.id
        )
    # kendi 50x'leri disarida kaldi -> yeterli kohort yok
    assert median is None or median < 50.0


def test_excess_separates_skill_from_market_weather(kohort):
    with session_scope() as s:
        med, _ = MK.cohort_median_multiple(
            s, "solana", NOW - timedelta(days=2),
            exclude_account_id=kohort["star"], exclude_call_id=kohort["star_call"],
        )
        star_excess = MK.excess_multiple(18.0, med)
        lag_excess = MK.excess_multiple(4.0, med)
    assert star_excess > 2.0      # kohortun iki katindan fazla
    assert lag_excess < 1.0       # kohortun altinda kaldi


def test_cohort_returns_none_when_too_few_samples(db):
    with session_scope() as s:
        median, n = MK.cohort_median_multiple(s, "solana", NOW)
    assert median is None
    assert n < settings.cohort_min_size


def test_excess_handles_missing_inputs():
    assert MK.excess_multiple(None, 5.0) is None
    assert MK.excess_multiple(5.0, None) is None
    assert MK.excess_multiple(5.0, 0) is None


# ------------------------------------------------------------- edge puani
def test_market_edge_zero_when_matching_the_crowd():
    assert MK.market_edge_score([1.0] * 5, [1.0] * 5) == 0.0
    assert MK.market_edge_score([0.5] * 5, [1.0] * 5) == 0.0


def test_market_edge_rewards_beating_the_crowd():
    az = MK.market_edge_score([1.5] * 5, [1.0] * 5)
    cok = MK.market_edge_score([3.0] * 5, [1.0] * 5)
    assert 0 < az < cok
    assert cok == pytest.approx(1.0, abs=0.01)


def test_market_edge_uses_median_not_mean():
    """Tek bir 20x kohort ustu, dokuz zayif cagriyi kurtaramaz."""
    tek_sansli = MK.market_edge_score([0.8] * 9 + [20.0], [1.0] * 10)
    istikrarli = MK.market_edge_score([2.0] * 10, [1.0] * 10)
    assert istikrarli > tek_sansli
    assert tek_sansli == 0.0


def test_market_edge_empty():
    assert MK.market_edge_score([], []) == 0.0
