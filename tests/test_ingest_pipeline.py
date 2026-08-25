"""Ingest boru hattinin uctan uca testi (agsiz).

tweet -> CA cikarimi -> DEX dogrulamasi -> Token/Call kaydi -> echo siralamasi
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from alpha_hunter.db.models import Account, Call, Token, TokenStatus, Tweet
from alpha_hunter.db.session import session_scope
from alpha_hunter.http import HttpClient
from alpha_hunter.ingest.base import RawTweet

MINT = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
PAIR = "8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj"
GARBAGE = "3QJmV3qfvL9SuYo34YihAf3sRCW3qSinyC6hpqg3Yqcd"   # gecerli base58, DEX'te yok
SUPPLY = 1_000_000_000
PRICE = 0.00005     # -> 50k MC

DEX_PAYLOAD = {
    "pairs": [
        {
            "chainId": "solana", "dexId": "raydium", "pairAddress": PAIR,
            "baseToken": {"address": MINT, "name": "Test Coin", "symbol": "TEST"},
            "quoteToken": {"symbol": "SOL"},
            "priceUsd": str(PRICE), "liquidity": {"usd": 120_000},
            "marketCap": PRICE * SUPPLY, "fdv": PRICE * SUPPLY,
            "volume": {"h24": 900_000}, "txns": {"h24": {"buys": 10, "sells": 5}},
            "pairCreatedAt": int((datetime.now(timezone.utc) - timedelta(hours=3)).timestamp() * 1000),
        }
    ]
}


class FakeHttp(HttpClient):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get(self, url, *, bucket="default", params=None, headers=None,
                  expect_json=True, max_retries=None):
        if "/latest/dex/tokens/" in url:
            requested = url.rsplit("/", 1)[-1].split(",")
            return DEX_PAYLOAD if MINT in requested else {"pairs": []}
        return None

    async def post(self, url, **kw):
        return None


class FakeSource:
    name = "fake"

    def __init__(self, tweets):
        self.tweets = tweets

    async def available(self):
        return True

    async def search(self, query, since, limit):
        for t in self.tweets:
            if t.posted_at >= since:
                yield t

    async def user_timeline(self, handle, since, limit):
        for t in self.tweets:
            if t.handle == handle and t.posted_at >= since:
                yield t


def _tweet(tid, handle, text, minutes_ago):
    return RawTweet(
        tweet_id=tid, handle=handle,
        posted_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        text=text, url=f"https://x.com/{handle}/status/{tid}", source="fake",
        followers=12_000,
    )


@pytest.fixture()
def patched(monkeypatch, db):
    tweets = [
        _tweet("1", "earlybird", f"stealth gem 🤫 CA: {MINT}", 2),
        _tweet("2", "latecomer", f"aped this https://pump.fun/coin/{MINT}", 1),
        _tweet("3", "noisemaker", f"random CA: {GARBAGE} trust me", 3),
        _tweet("4", "chatter", "gm frens, beautiful day to be alive", 4),
        _tweet("5", "earlybird", f"still holding CA: {MINT}", 1),   # ayni token, ayni hesap
    ]
    monkeypatch.setattr("alpha_hunter.pipeline.ingest.HttpClient", FakeHttp)
    monkeypatch.setattr(
        "alpha_hunter.pipeline.ingest.build_sources", lambda http, names=None: [FakeSource(tweets)]
    )
    return tweets


async def test_ingest_creates_accounts_tweets_and_calls(patched):
    from alpha_hunter.pipeline.ingest import run_ingest

    stats = await run_ingest(lookback_minutes=60, queries=["test"], handles=[])
    assert stats["tweets_seen"] == 5
    assert stats["calls_new"] == 3          # earlybird x2, latecomer x1
    assert stats["rejected"] == 1           # GARBAGE

    with session_scope() as s:
        handles = {a.handle for a in s.scalars(select(Account))}
        # sadece CA iceren tweetlerin sahipleri kaydedilir
        assert handles == {"earlybird", "latecomer", "noisemaker"}
        assert s.scalar(select(Tweet).where(Tweet.platform_tweet_id == "4")) is None

        tok = s.scalar(select(Token).where(Token.address == MINT))
        assert tok.status == TokenStatus.ACTIVE
        assert tok.symbol == "TEST"
        assert tok.supply_estimate == pytest.approx(SUPPLY, rel=1e-6)
        assert tok.pair_address == PAIR

        junk = s.scalar(select(Token).where(Token.address == GARBAGE))
        assert junk.status == TokenStatus.INVALID
        assert s.scalar(select(Call).where(Call.token_id == junk.id)) is None


async def test_fresh_tweet_gets_live_entry_price(patched):
    from alpha_hunter.pipeline.ingest import run_ingest

    await run_ingest(lookback_minutes=60, queries=["test"], handles=[])
    with session_scope() as s:
        tw = s.scalar(select(Tweet).where(Tweet.platform_tweet_id == "1"))
        call = s.scalar(select(Call).where(Call.tweet_id == tw.id))
        # Tweet dakikalar oncesine ait -> anlik fiyat T1 kabul edilir
        assert call.entry_source == "live_at_ingest"
        assert call.entry_price_usd == pytest.approx(PRICE)
        assert call.entry_mc_usd == pytest.approx(PRICE * SUPPLY)
        assert call.entry_confidence >= 0.70
        assert call.mc_earliness is not None and call.mc_earliness > 0.4


async def test_caller_ranks_reflect_discovery_order(patched):
    from alpha_hunter.pipeline.ingest import run_ingest

    await run_ingest(lookback_minutes=60, queries=["test"], handles=[])
    with session_scope() as s:
        tok = s.scalar(select(Token).where(Token.address == MINT))
        calls = list(
            s.scalars(select(Call).where(Call.token_id == tok.id).order_by(Call.called_at))
        )
        assert len(calls) == 3
        first = calls[0]
        acc = s.get(Account, first.account_id)
        assert acc.handle == "earlybird"           # 2 dakika once -> ilk
        assert first.caller_rank == 1
        assert first.originality == 1.0
        # latecomer ikinci hesap
        second = next(c for c in calls if s.get(Account, c.account_id).handle == "latecomer")
        assert second.caller_rank == 2
        assert second.originality < 1.0


async def test_ingest_is_idempotent(patched):
    from alpha_hunter.pipeline.ingest import run_ingest

    await run_ingest(lookback_minutes=60, queries=["test"], handles=[])
    second = await run_ingest(lookback_minutes=60, queries=["test"], handles=[])
    assert second["tweets_new"] == 0
    assert second["calls_new"] == 0
    with session_scope() as s:
        assert len(list(s.scalars(select(Call)))) == 3
