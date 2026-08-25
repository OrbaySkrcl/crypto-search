from alpha_hunter.ingest.extractor import (
    extract_cashtags,
    extract_contract_addresses,
    is_valid_solana_address,
    looks_like_alpha_tweet,
)

SOL = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
SOL2 = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
EVM = "0x532f27101965dd16442E59d40670FaF5eBB142E4"


def test_base58_validation():
    assert is_valid_solana_address(SOL)
    assert is_valid_solana_address(SOL2)
    # islem imzasi (88 karakter) adres degildir
    assert not is_valid_solana_address("5" * 88)
    # base58 disi karakterler (0, O, I, l)
    assert not is_valid_solana_address("0OIl" + SOL[4:])
    assert not is_valid_solana_address("kisa")


def test_context_extraction():
    got = extract_contract_addresses(f"new gem CA: {SOL} ape now", chains=["solana"])
    assert len(got) == 1
    assert got[0].address == SOL
    assert got[0].method == "context"
    assert got[0].confidence > 0.8


def test_bare_address_lower_confidence():
    ctx = extract_contract_addresses(f"contract {SOL}", chains=["solana"])[0]
    bare = extract_contract_addresses(f"gm frens {SOL} lfg", chains=["solana"])[0]
    assert ctx.confidence > bare.confidence


def test_pumpfun_url_is_highest_confidence():
    got = extract_contract_addresses(f"https://pump.fun/coin/{SOL}", chains=["solana"])
    assert got[0].confidence >= 0.99
    assert got[0].method == "url"


def test_dexscreener_url_marks_pair_hint():
    got = extract_contract_addresses(f"https://dexscreener.com/solana/{SOL2}", chains=["solana"])
    assert got[0].pair_hint == SOL2


def test_denylist_filters_known_tokens():
    text = "So11111111111111111111111111111111111111112 and EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    assert extract_contract_addresses(text, chains=["solana"]) == []


def test_evm_denylist_and_normalisation():
    assert extract_contract_addresses(
        "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", chains=["ethereum"]
    ) == []
    got = extract_contract_addresses(f"contract: {EVM}", chains=["base"])
    assert got[0].address == EVM.lower()   # EVM adresleri kucuk harfe normalize edilir


def test_url_addresses_not_double_counted_as_bare():
    got = extract_contract_addresses(f"https://pump.fun/coin/{SOL}", chains=["solana"])
    assert len(got) == 1


def test_cashtags():
    assert extract_cashtags("$WIF and $BONK to the moon") == ["BONK", "WIF"]
    assert extract_cashtags("https://x.com/$fake") == []


def test_prefilter():
    assert looks_like_alpha_tweet(f"CA: {SOL}")
    assert looks_like_alpha_tweet("https://pump.fun/coin/whatever")
    assert not looks_like_alpha_tweet("gm everyone, beautiful morning")
    assert not looks_like_alpha_tweet("")
