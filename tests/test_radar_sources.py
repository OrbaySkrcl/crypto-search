"""Radar kaynak ayristiricilari — gercek API govde sekilleriyle, agsiz.

Buradaki JSON'lar GeckoTerminal v2, DexScreener, RugCheck ve GoPlus'in
belgelenmis govde sekilleridir. Ag erisimi olmadan ayristiricinin dogru
calistigi ve kaynak dustugunde botun cokmedigi burada dogrulanir.
"""
from __future__ import annotations

import pytest

from radar.sources.dexscreener import DexScreener
from radar.sources.geckoterminal import GeckoTerminal
from radar.sources.safety import SafetyReport, check_token, structural_check

MINT = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
PAIR = "8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj"

GT_POOLS = {
    "data": [
        {
            "id": f"solana_{PAIR}",
            "type": "pool",
            "attributes": {
                "name": "MOON / SOL",
                "address": PAIR,
                "base_token_price_usd": "0.00042",
                "reserve_in_usd": "84000.5",
                "fdv_usd": "420000",
                "market_cap_usd": "410000",
                "pool_created_at": "2025-03-01T00:00:00Z",
                "volume_usd": {"m5": "1000", "h1": "52000", "h6": "180000", "h24": "240000"},
                "transactions": {"h1": {"buys": 210, "sells": 90}},
            },
            "relationships": {
                "base_token": {"data": {"id": f"solana_{MINT}", "type": "token"}},
                "dex": {"data": {"id": "raydium", "type": "dex"}},
            },
        },
        {   # alanlari eksik havuz -> dusmemeli, alanlar None kalmali
            "id": "solana_XXXX",
            "type": "pool",
            "attributes": {"name": "JUNK / SOL", "address": "XXXX"},
            "relationships": {"base_token": {"data": {"id": "solana_JUNKMINT"}}},
        },
    ]
}

GT_TRADES = {
    "data": [
        {
            "id": "t1",
            "type": "trade",
            "attributes": {
                "block_number": 1,
                "tx_hash": "sig-aaa",
                "tx_from_address": "WalletAAA",
                "block_timestamp": "2025-03-01T01:00:00Z",
                "kind": "buy",
                "volume_in_usd": "1500.0",
                "price_to_in_usd": "0.0004",
                "price_from_in_usd": "140.0",
            },
        },
        {
            "id": "t2",
            "type": "trade",
            "attributes": {
                "tx_hash": "sig-bbb",
                "tx_from_address": "WalletBBB",
                "block_timestamp": 1740790800,          # unix saniye
                "kind": "sell",
                "volume_in_usd": "900.0",
                "price_from_in_usd": "0.0009",
            },
        },
        {   # cuzdan adresi yok -> elenmeli
            "id": "t3",
            "type": "trade",
            "attributes": {"tx_hash": "sig-ccc", "kind": "buy",
                           "block_timestamp": "2025-03-01T01:05:00Z"},
        },
    ]
}

DS_TOKENS = {
    "schemaVersion": "1.0.0",
    "pairs": [
        {
            "chainId": "solana",
            "dexId": "raydium",
            "pairAddress": PAIR,
            "baseToken": {"address": MINT, "name": "Moon Token", "symbol": "MOON"},
            "quoteToken": {"symbol": "SOL"},
            "priceUsd": "0.00042",
            "liquidity": {"usd": 84000.5},
            "fdv": 420000,
            "marketCap": 410000,
            "volume": {"h24": 240000, "h1": 52000},
            "priceChange": {"h1": 12.4, "h24": -3.1},
            "txns": {"h1": {"buys": 210, "sells": 90}},
            "pairCreatedAt": 1740787200000,
        },
        {   # ayni token, daha az likit havuz -> secilmemeli
            "chainId": "solana",
            "dexId": "orca",
            "pairAddress": "OTHERPAIR",
            "baseToken": {"address": MINT, "symbol": "MOON"},
            "priceUsd": "0.00050",
            "liquidity": {"usd": 4000},
            "marketCap": 500000,
        },
    ],
}


@pytest.mark.asyncio
async def test_gt_new_pools_parses(fake_http):
    fake_http.add("/new_pools", GT_POOLS)
    pools = await GeckoTerminal(fake_http).new_pools("solana", pages=1)

    assert len(pools) == 2
    p = pools[0]
    assert p.token_address == MINT
    assert p.pair_address == PAIR
    assert p.symbol == "MOON"
    assert p.dex_id == "raydium"
    assert p.liquidity_usd == pytest.approx(84000.5)
    assert p.mc_usd == pytest.approx(410000)
    assert p.volume_h1 == pytest.approx(52000)
    assert p.created_at is not None and p.created_at.year == 2025
    assert pools[1].liquidity_usd is None


@pytest.mark.asyncio
async def test_gt_trades_extracts_wallets(fake_http):
    fake_http.add("/trades", GT_TRADES)
    trades = await GeckoTerminal(fake_http).trades("solana", PAIR)

    assert [t.wallet for t in trades] == ["WalletAAA", "WalletBBB"]
    assert trades[0].side == "buy"
    assert trades[0].usd == pytest.approx(1500.0)
    assert trades[0].price_usd == pytest.approx(0.0004)     # alimda "to" tarafi
    assert trades[1].side == "sell"
    assert trades[1].price_usd == pytest.approx(0.0009)     # satista "from" tarafi
    assert trades[1].at.tzinfo is not None                  # unix -> UTC


@pytest.mark.asyncio
async def test_dexscreener_picks_most_liquid_pool(fake_http):
    fake_http.add("/latest/dex/tokens/", DS_TOKENS)
    snaps = await DexScreener(fake_http).tokens([MINT], chain="solana")

    s = snaps[MINT]
    assert s.symbol == "MOON"
    assert s.liquidity_usd == pytest.approx(84000.5)        # 4000'lik havuz degil
    assert s.pair_address == PAIR
    assert s.buy_pressure == pytest.approx(210 / 300)
    assert s.pair_created_at is not None


@pytest.mark.asyncio
async def test_source_failure_returns_empty_not_crash(fake_http):
    """Bedava kaynaklar duser. Dustugunde bot cokmemeli."""
    assert await GeckoTerminal(fake_http).new_pools("solana") == []
    assert await GeckoTerminal(fake_http).trades("solana", PAIR) == []
    assert await DexScreener(fake_http).tokens([MINT]) == {}


# --------------------------------------------------------------------------- #
#  Guvenlik kapisi
# --------------------------------------------------------------------------- #
def test_structural_check_flags_honeypot_pattern():
    rep = SafetyReport(chain="solana", address=MINT)
    structural_check(rep, liquidity_usd=60_000, mc_usd=300_000, age_minutes=600,
                     buy_pressure=0.99, volume_h24=100_000)
    assert any("honeypot" in f for f in rep.flags)


def test_structural_check_rewards_deep_liquidity():
    clean = SafetyReport(chain="solana", address=MINT)
    structural_check(clean, liquidity_usd=200_000, mc_usd=1_000_000,
                     age_minutes=5000, buy_pressure=0.55, volume_h24=500_000)
    thin = SafetyReport(chain="solana", address=MINT)
    structural_check(thin, liquidity_usd=3_000, mc_usd=1_000_000,
                     age_minutes=5, buy_pressure=0.55, volume_h24=500)
    assert clean.score > thin.score
    assert thin.score < 0.4


@pytest.mark.asyncio
async def test_rugcheck_mint_authority_penalised(fake_http):
    fake_http.add("/report/summary", {
        "score_normalised": 40,
        "risks": [
            {"name": "Mint Authority still enabled", "level": "danger"},
            {"name": "Low amount of LP Providers", "level": "warn"},
        ],
    })
    rep = await check_token(fake_http, "solana", MINT, liquidity_usd=50_000,
                            mc_usd=300_000, age_minutes=600)
    assert "rugcheck" in rep.sources
    assert any("Mint Authority" in f for f in rep.flags)
    assert rep.score < 0.5


@pytest.mark.asyncio
async def test_goplus_honeypot_is_fatal(fake_http):
    fake_http.add("/token_security/", {
        "code": 1,
        "result": {"0xabc": {"is_honeypot": "1", "buy_tax": "0", "sell_tax": "0.99",
                             "is_open_source": "1", "holder_count": "800"}},
    })
    rep = await check_token(fake_http, "ethereum", "0xABC", liquidity_usd=90_000,
                            mc_usd=500_000, age_minutes=900)
    assert rep.fatal is True
    assert rep.score < 0.2


@pytest.mark.asyncio
async def test_no_external_source_is_flagged_not_silent(fake_http):
    """Kaynak cevap vermezse kart YUTULMAZ; bayrak dusulur ve karta yazilir."""
    rep = await check_token(fake_http, "solana", MINT, liquidity_usd=90_000,
                            mc_usd=500_000, age_minutes=900)
    assert rep.sources == []
    assert any("cevap vermedi" in f for f in rep.flags)
    assert rep.score > 0
