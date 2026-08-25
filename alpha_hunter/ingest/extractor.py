"""Kontrat Adresi (CA) cikarici.

Bu katmanin isi YUKSEK RECALL, orta precision: adayi yakala, dogrulamayi
on-chain katmanina birak. Yanlis pozitif (cuzdan adresi, imza, rastgele string)
DEX'te bulunamayacagi icin `TokenStatus.INVALID` olarak elenir ve hicbir zaman
skora girmez.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------- #
#  Base58 (Bitcoin alfabesi) -- harici bagimlilik olmadan
# --------------------------------------------------------------------------- #
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def b58decode(s: str) -> bytes | None:
    num = 0
    for ch in s:
        idx = _B58_INDEX.get(ch)
        if idx is None:
            return None
        num = num * 58 + idx
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def is_valid_solana_address(s: str) -> bool:
    """Gecerli bir Solana pubkey'i tam olarak 32 bayta cozulur."""
    if not (32 <= len(s) <= 44):
        return False
    decoded = b58decode(s)
    return decoded is not None and len(decoded) == 32


# --------------------------------------------------------------------------- #
#  Desenler
# --------------------------------------------------------------------------- #
SOLANA_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])([1-9A-HJ-NP-Za-km-z]{32,44})(?![1-9A-HJ-NP-Za-km-z])")
EVM_RE = re.compile(r"(?<![0-9a-fA-Fx])(0x[a-fA-F0-9]{40})(?![0-9a-fA-F])")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9])\$([A-Za-z][A-Za-z0-9_]{1,14})\b")

# "CA:", "contract:", "mint:", "token:" gibi baglam ipuclari
CONTEXT_RE = re.compile(
    r"\b(ca|c\.?a\.?|contract|contract\s*address|mint|token\s*address|address)\b\s*[:\-=]?",
    re.I,
)

# --------------------------------------------------------------------------- #
#  Deny list -- token OLMAYAN, cok gecen adresler
# --------------------------------------------------------------------------- #
SOLANA_DENY = {
    "So11111111111111111111111111111111111111112",   # wSOL
    "So11111111111111111111111111111111111111111",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # Token Program
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",   # Token-2022
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # ATA program
    "11111111111111111111111111111111",              # System Program
    "ComputeBudget111111111111111111111111111111",
    "SysvarRent111111111111111111111111111111111",
    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",   # Metaplex
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",   # Jupiter v6
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",   # Pump.fun program
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",   # PumpSwap
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",   # Meteora DLMM
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",  # Meteora
    "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
}
EVM_DENY = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
    "0x4200000000000000000000000000000000000006",  # WETH (Base/OP)
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
    "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC Base
    "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d",  # Uniswap V2 router
}

CHAIN_ALIASES = {
    "solana": "solana", "sol": "solana",
    "ethereum": "ethereum", "eth": "ethereum", "mainnet": "ethereum",
    "base": "base",
    "bsc": "bsc", "binance": "bsc", "bnb": "bsc",
    "arbitrum": "arbitrum", "arb": "arbitrum",
    "polygon": "polygon", "matic": "polygon",
}


@dataclass(frozen=True)
class ExtractedCA:
    chain: str
    address: str
    confidence: float          # 0..1 -- bu gercekten bir token CA'si mi?
    method: str                # 'url' | 'context' | 'bare'
    pair_hint: str | None = None   # bazi URL'ler token degil PAIR adresi verir


# --------------------------------------------------------------------------- #
#  URL tabanli cikarim (en yuksek guven)
# --------------------------------------------------------------------------- #
def _from_url(raw: str) -> list[ExtractedCA]:
    out: list[ExtractedCA] = []
    try:
        u = urlparse(raw)
    except Exception:
        return out
    host = (u.netloc or "").lower().removeprefix("www.")
    parts = [p for p in (u.path or "").split("/") if p]
    qs = parse_qs(u.query or "")

    def add(chain: str, addr: str, conf: float, pair: str | None = None) -> None:
        c = _normalise(chain, addr)
        if c:
            out.append(ExtractedCA(c[0], c[1], conf, "url", pair))

    # --- Solana odakli terminaller ------------------------------------- #
    if "pump.fun" in host:
        # pump.fun/coin/<mint>  |  pump.fun/<mint>
        for p in parts:
            if p != "coin" and is_valid_solana_address(p):
                add("solana", p, 0.99)
    elif "dexscreener.com" in host:
        # /<chain>/<pairAddress>  -- token degil PAIR adresi olabilir
        if len(parts) >= 2 and parts[0].lower() in CHAIN_ALIASES:
            chain = CHAIN_ALIASES[parts[0].lower()]
            cand = parts[1]
            add(chain, cand, 0.90, pair=cand)
    elif "birdeye.so" in host:
        chain = CHAIN_ALIASES.get((qs.get("chain") or ["solana"])[0].lower(), "solana")
        for p in parts:
            if p not in ("token", "tv") and _looks_like_address(p):
                add(chain, p, 0.97)
    elif "gmgn.ai" in host:
        # gmgn.ai/sol/token/<mint>  |  gmgn.ai/base/token/0x..
        chain = CHAIN_ALIASES.get(parts[0].lower(), "solana") if parts else "solana"
        for p in parts[1:]:
            if p != "token" and _looks_like_address(p):
                add(chain, p, 0.97)
    elif "photon-sol" in host or "photon.tinyastro" in host:
        for p in parts:
            if p not in ("en", "lp", "memescope") and is_valid_solana_address(p):
                add("solana", p, 0.90, pair=p)
    elif "bullx.io" in host or "neo.bullx" in host:
        for key in ("address", "token", "mint"):
            for v in qs.get(key, []):
                if _looks_like_address(v):
                    add("solana" if is_valid_solana_address(v) else "ethereum", v, 0.95)
    elif "axiom.trade" in host:
        for p in parts:
            if is_valid_solana_address(p):
                add("solana", p, 0.95)
    elif "solscan.io" in host or "solana.fm" in host or "explorer.solana.com" in host:
        for p in parts:
            if p not in ("token", "account", "address") and is_valid_solana_address(p):
                add("solana", p, 0.85)
    elif "jup.ag" in host:
        for p in parts:
            for seg in p.split("-"):
                if is_valid_solana_address(seg):
                    add("solana", seg, 0.85)
    elif "raydium.io" in host:
        for key in ("outputMint", "inputMint", "outputCurrency"):
            for v in qs.get(key, []):
                if is_valid_solana_address(v):
                    add("solana", v, 0.85)
    # --- EVM tarayicilar ------------------------------------------------ #
    elif "etherscan.io" in host:
        _scan_evm(parts, "ethereum", add)
    elif "basescan.org" in host:
        _scan_evm(parts, "base", add)
    elif "bscscan.com" in host:
        _scan_evm(parts, "bsc", add)
    elif "arbiscan.io" in host:
        _scan_evm(parts, "arbitrum", add)
    elif "polygonscan.com" in host:
        _scan_evm(parts, "polygon", add)
    elif "dextools.io" in host:
        chain = next((CHAIN_ALIASES[p.lower()] for p in parts if p.lower() in CHAIN_ALIASES), None)
        for p in parts:
            if _looks_like_address(p) and chain:
                add(chain, p, 0.85, pair=p)

    return out


def _scan_evm(parts: list[str], chain: str, add) -> None:
    for p in parts:
        if EVM_RE.fullmatch(p):
            add(chain, p, 0.90)


def _looks_like_address(s: str) -> bool:
    return bool(EVM_RE.fullmatch(s)) or is_valid_solana_address(s)


def _normalise(chain: str, addr: str) -> tuple[str, str] | None:
    chain = CHAIN_ALIASES.get(chain.lower(), chain.lower())
    if chain == "solana":
        if not is_valid_solana_address(addr) or addr in SOLANA_DENY:
            return None
        return ("solana", addr)
    if EVM_RE.fullmatch(addr):
        low = addr.lower()
        if low in EVM_DENY:
            return None
        return (chain, low)
    return None


# --------------------------------------------------------------------------- #
#  Ana giris noktasi
# --------------------------------------------------------------------------- #
def extract_contract_addresses(
    text: str,
    *,
    chains: list[str] | None = None,
    expanded_urls: list[str] | None = None,
) -> list[ExtractedCA]:
    """Metinden CA adaylarini cikarir, en yuksek guvenli olani her adres icin tutar.

    `expanded_urls`: t.co kisaltmalarinin cozulmus halleri (varsa).
    """
    allowed = set(chains) if chains else None
    found: dict[tuple[str, str], ExtractedCA] = {}

    def keep(item: ExtractedCA) -> None:
        if allowed and item.chain not in allowed:
            return
        key = (item.chain, item.address)
        prev = found.get(key)
        if prev is None or item.confidence > prev.confidence:
            found[key] = item if prev is None else ExtractedCA(
                item.chain, item.address, item.confidence, item.method,
                item.pair_hint or prev.pair_hint,
            )

    # 1) URL'ler -- en guvenilir kaynak
    urls = URL_RE.findall(text or "")
    for u in urls + list(expanded_urls or []):
        for item in _from_url(u):
            keep(item)

    # URL'leri metinden cikar ki icindeki adresler "bare" olarak tekrar sayilmasin
    stripped = URL_RE.sub(" ", text or "")

    # 2) Baglam ipucuyla ("CA: xyz") gecen adresler
    ctx_spans: list[tuple[int, int]] = [(m.end(), m.end() + 120) for m in CONTEXT_RE.finditer(stripped)]

    def in_context(pos: int) -> bool:
        return any(a <= pos <= b for a, b in ctx_spans)

    for m in EVM_RE.finditer(stripped):
        addr = m.group(1)
        norm = _normalise("ethereum", addr)
        if not norm:
            continue
        conf = 0.85 if in_context(m.start()) else 0.60
        method = "context" if in_context(m.start()) else "bare"
        # EVM'de zincir metinden anlasilmaz -> yapilandirilmis zincirlerin
        # her birine aday olarak yazilir, dogrulamayi DEX yapar.
        for ch in (chains or ["ethereum", "base", "bsc"]):
            if ch == "solana":
                continue
            keep(ExtractedCA(ch, norm[1], conf, method))

    for m in SOLANA_RE.finditer(stripped):
        addr = m.group(1)
        norm = _normalise("solana", addr)
        if not norm:
            continue
        conf = 0.88 if in_context(m.start()) else 0.55
        keep(ExtractedCA("solana", norm[1], conf, "context" if in_context(m.start()) else "bare"))

    return sorted(found.values(), key=lambda c: -c.confidence)


def extract_cashtags(text: str) -> list[str]:
    """$TICKER etiketleri. CA degil ama 'hangi coin' eslesmesinde yardimci."""
    stripped = URL_RE.sub(" ", text or "")
    return sorted({m.group(1).upper() for m in CASHTAG_RE.finditer(stripped)})


def looks_like_alpha_tweet(text: str) -> bool:
    """Ucuz on-filtre: pahalii CA dogrulamasindan once caginlir."""
    if not text:
        return False
    t = text.lower()
    if "pump.fun" in t or "dexscreener" in t or "birdeye" in t or "gmgn.ai" in t:
        return True
    if CONTEXT_RE.search(text):
        return True
    return bool(EVM_RE.search(text) or SOLANA_RE.search(text))
