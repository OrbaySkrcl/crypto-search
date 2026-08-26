"""Zincir uzeri cuzdan avi testleri (agsiz)."""
import itertools
from datetime import datetime, timedelta, timezone

import pytest

from alpha_hunter.config import settings
from alpha_hunter.db.models import (
    Account,
    Call,
    CallOutcome,
    CallSource,
    Token,
    TokenStatus,
    Tweet,
    Wallet,
)
from alpha_hunter.db.session import session_scope
from alpha_hunter.onchain import linker
from alpha_hunter.onchain.profiler import classify_wallet, create_wallet_call, upsert_wallet
from alpha_hunter.onchain.trades import normalise_trade
from alpha_hunter.onchain.types import BuyEvent
from alpha_hunter.scoring.engine import score_wallet

NOW = datetime.now(timezone.utc)
MINT = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
_N = itertools.count(1)


# --------------------------------------------------------------- islem cozme
def _trade(**kw):
    base = {
        "owner": "Wa11et1111111111111111111111111111111111111",
        "blockUnixTime": int((NOW - timedelta(hours=3)).timestamp()),
        "side": "buy", "volumeUsd": 250.0, "priceUsd": 0.00004,
        "txHash": f"sig{next(_N)}",
    }
    base.update(kw)
    return base


def test_buy_is_parsed():
    ev = normalise_trade(_trade(), MINT, "solana")
    assert ev is not None
    assert ev.usd == 250.0
    assert ev.price_usd == 0.00004
    assert ev.at < NOW


def test_sell_is_rejected():
    """Satis alfa sinyali degil."""
    assert normalise_trade(_trade(side="sell"), MINT, "solana") is None


def test_side_inferred_from_token_direction():
    """side alani yoksa hedef token hangi tarafta ondan cikarilir."""
    alim = _trade(side="", to={"address": MINT}, from_={"address": "SOL"})
    alim["to"] = {"address": MINT}
    del alim["side"]
    assert normalise_trade(alim, MINT, "solana") is not None

    satis = _trade(side="")
    satis["to"] = {"address": "So11111111111111111111111111111111111111112"}
    del satis["side"]
    assert normalise_trade(satis, MINT, "solana") is None


@pytest.mark.parametrize("alan", ["owner", "wallet", "trader", "signer"])
def test_wallet_field_aliases(alan):
    row = _trade()
    adres = row.pop("owner")
    row[alan] = adres
    ev = normalise_trade(row, MINT, "solana")
    assert ev is not None and ev.wallet == adres


def test_millisecond_timestamps():
    ev = normalise_trade(
        _trade(blockUnixTime=int((NOW - timedelta(hours=1)).timestamp() * 1000)),
        MINT, "solana",
    )
    assert ev is not None
    assert abs((NOW - ev.at).total_seconds() - 3600) < 5


def test_garbage_rows_are_rejected():
    assert normalise_trade({}, MINT, "solana") is None
    assert normalise_trade({"owner": "x"}, MINT, "solana") is None      # zaman yok
    assert normalise_trade("metin", MINT, "solana") is None


# ------------------------------------------------------------- bot elemesi
def _token(s, yas_saat=5):
    tok = Token(chain="solana", address=f"TOK{next(_N):041d}",
                status=TokenStatus.ACTIVE, supply_estimate=1e9,
                pair_created_at=NOW - timedelta(hours=yas_saat))
    s.add(tok)
    s.flush()
    return tok


def _buy(s, w, tok, gecikme_sn, usd=200.0):
    ev = BuyEvent(wallet=w.address, token_address=tok.address, chain="solana",
                  at=NOW - timedelta(hours=5) + timedelta(seconds=gecikme_sn),
                  usd=usd, price_usd=0.00005, tx=f"tx{next(_N)}")
    return create_wallet_call(s, w, tok, ev)[0]


def test_sniper_bot_is_flagged(db):
    """Her havuza ilk saniyelerde giren, insan degil bottur."""
    with session_scope() as s:
        w = upsert_wallet(s, "solana", "BotWa11et11111111111111111111111111111111")
        for _ in range(6):
            _buy(s, w, _token(s), gecikme_sn=3)
        classify_wallet(s, w)
        assert w.is_bot is True
        assert "sniper" in (w.bot_reason or "")


def test_human_timing_is_not_flagged(db):
    with session_scope() as s:
        w = upsert_wallet(s, "solana", "Human1111111111111111111111111111111111111")
        for i in range(6):
            _buy(s, w, _token(s), gecikme_sn=600 + i * 300)
        classify_wallet(s, w)
        assert w.is_bot is False
        assert w.median_entry_delay_sec > 300


def test_spray_bot_is_flagged_by_token_count(db, monkeypatch):
    monkeypatch.setattr(settings, "wallet_bot_token_threshold", 5)
    with session_scope() as s:
        w = upsert_wallet(s, "solana", "Spray111111111111111111111111111111111111")
        for _ in range(6):
            _buy(s, w, _token(s), gecikme_sn=900)
        classify_wallet(s, w)
        assert w.is_bot is True
        assert "tarama botu" in (w.bot_reason or "")


def test_bot_score_is_zeroed(db):
    with session_scope() as s:
        w = upsert_wallet(s, "solana", "Bot2222222222222222222222222222222222222")
        for _ in range(6):
            c = _buy(s, w, _token(s), gecikme_sn=2)
            c.outcome = CallOutcome.WIN
            c.sustained_multiple = 50.0
            c.max_multiple = 50.0
            c.is_closed = True
        classify_wallet(s, w)
        res = score_wallet(s, w)
        assert res.alpha_score == 0.0        # 50x kazanan bot bile sifir
        assert res.tier == "F"


def test_wallet_call_records_entry_from_chain_price(db):
    """Zincirdeki islem fiyati kesindir -- OHLCV tahmininden iyi."""
    with session_scope() as s:
        w = upsert_wallet(s, "solana", "Buyer111111111111111111111111111111111111")
        tok = _token(s)
        c = _buy(s, w, tok, gecikme_sn=1200)
        assert c.source == CallSource.WALLET
        assert c.entry_price_usd == 0.00005
        assert c.entry_mc_usd == pytest.approx(50_000)
        assert c.entry_confidence == 1.0
        assert c.token_age_at_call_sec == pytest.approx(1200, abs=5)
        assert c.buy_usd == 200.0


def test_same_transaction_is_not_counted_twice(db):
    with session_scope() as s:
        w = upsert_wallet(s, "solana", "Dup11111111111111111111111111111111111111")
        tok = _token(s)
        ev = BuyEvent(wallet=w.address, token_address=tok.address, chain="solana",
                      at=NOW, usd=100.0, price_usd=1e-5, tx="ayni-tx")
        _c1, yeni1 = create_wallet_call(s, w, tok, ev)
        _c2, yeni2 = create_wallet_call(s, w, tok, ev)
        assert yeni1 is True and yeni2 is False


# ------------------------------------------- ASAMA 3: cuzdan <-> hesap
def _tweet_call(s, acc, tok, when):
    tw = Tweet(account_id=acc.id, platform_tweet_id=f"tw{next(_N)}",
               posted_at=when, text="CA:", source="test")
    s.add(tw)
    s.flush()
    c = Call(source=CallSource.TWEET, account_id=acc.id, tweet_id=tw.id,
             token_id=tok.id, chain="solana", called_at=when)
    s.add(c)
    s.flush()
    return c


@pytest.fixture()
def insider(db):
    """Bir cuzdan, hep @sinyalci'nin tweetinden ~5 dakika once aliyor."""
    with session_scope() as s:
        acc = Account(platform="x", handle="sinyalci")
        s.add(acc)
        s.flush()
        w = upsert_wallet(s, "solana", "Insider1111111111111111111111111111111111")
        baska = upsert_wallet(s, "solana", "Takipci1111111111111111111111111111111111")

        for i in range(5):
            tok = _token(s)
            tweet_at = NOW - timedelta(days=i + 1)
            # insider once alir
            create_wallet_call(s, w, tok, BuyEvent(
                wallet=w.address, token_address=tok.address, chain="solana",
                at=tweet_at - timedelta(minutes=5), usd=500.0, price_usd=1e-5,
                tx=f"ins{next(_N)}"))
            # takipci tweetten SONRA alir
            create_wallet_call(s, baska, tok, BuyEvent(
                wallet=baska.address, token_address=tok.address, chain="solana",
                at=tweet_at + timedelta(minutes=8), usd=300.0, price_usd=3e-5,
                tx=f"tak{next(_N)}"))
            _tweet_call(s, acc, tok, tweet_at)
        return {"wallet": w.address, "follower": baska.address, "handle": "sinyalci"}


def test_wallet_buying_before_tweets_is_linked(insider):
    with session_scope() as s:
        links = linker.find_links(s)
    assert links, "tweetten once alan cuzdan bulunamadi"
    top = links[0]
    assert top["token_count"] == 5
    assert 250 <= top["median_lead_sec"] <= 350      # ~5 dakika once
    assert top["confidence"] > 0.5


def test_follower_who_buys_after_is_not_linked(insider):
    """Tweetten SONRA alan takipcidir, insider degil."""
    with session_scope() as s:
        links = linker.find_links(s)
        w = s.scalar(select_wallet(insider["follower"]))
        assert all(link["wallet_id"] != w.id for link in links)


def select_wallet(address):
    from sqlalchemy import select
    return select(Wallet).where(Wallet.address == address)


def test_links_are_written_to_the_wallet(insider):
    with session_scope() as s:
        linker.run_linking(s)
    with session_scope() as s:
        w = s.scalar(select_wallet(insider["wallet"]))
        assert w.linked_account_id is not None
        assert w.link_confidence > 0.5
        assert w.link_evidence["handle"] == "sinyalci"
        assert w.link_evidence["token_count"] == 5


def test_single_coincidence_is_not_enough(db):
    """Tek tokende once almak tesaduf olabilir; desen sart."""
    with session_scope() as s:
        acc = Account(platform="x", handle="tek")
        s.add(acc)
        s.flush()
        w = upsert_wallet(s, "solana", "Sans11111111111111111111111111111111111111")
        tok = _token(s)
        create_wallet_call(s, w, tok, BuyEvent(
            wallet=w.address, token_address=tok.address, chain="solana",
            at=NOW - timedelta(minutes=10), usd=100.0, price_usd=1e-5, tx="t1"))
        _tweet_call(s, acc, tok, NOW - timedelta(minutes=5))
        assert linker.find_links(s) == []


def test_buying_long_before_is_not_evidence(db):
    """Iki gun once almak, tweetle iliskili sayilmaz."""
    with session_scope() as s:
        acc = Account(platform="x", handle="uzak")
        s.add(acc)
        s.flush()
        w = upsert_wallet(s, "solana", "Uzak11111111111111111111111111111111111111")
        for _ in range(4):
            tok = _token(s)
            tweet_at = NOW - timedelta(days=3)
            create_wallet_call(s, w, tok, BuyEvent(
                wallet=w.address, token_address=tok.address, chain="solana",
                at=tweet_at - timedelta(days=2), usd=100.0, price_usd=1e-5,
                tx=f"u{next(_N)}"))
            _tweet_call(s, acc, tok, tweet_at)
        assert linker.find_links(s) == []


def test_confidence_grows_with_repetition():
    az = linker._confidence(3, 300, 0.2, 7200)
    cok = linker._confidence(10, 300, 0.2, 7200)
    assert cok > az


def test_confidence_rewards_buying_sooner():
    hemen = linker._confidence(5, 60, 0.2, 7200)
    gec = linker._confidence(5, 6000, 0.2, 7200)
    assert hemen > gec
