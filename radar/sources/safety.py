"""Guvenlik kapisi — alarm gonderilmeden ONCE calisir.

Kazandiran botla kaybettiren bot arasindaki fark cogu zaman burasidir:
sinyal dogru olsa bile token honeypot ise sonuc sifirdir.

Kaynaklar (hepsi bedava, anahtarsiz):
  * RugCheck.xyz  — Solana risk raporu
  * GoPlus Labs   — EVM + Solana token guvenligi (honeypot, vergi, sahiplik)
  * Yapisal       — likidite, havuz yasi, alim/satim dengesi (DexScreener'dan)

Sonuc 0..1 arasi tek bir sayi ve insan okunur bayraklardir. Yorum yok,
"yapay zeka degerlendirmesi" yok — kontrol listesi var.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import GOPLUS_CHAIN_ID
from ..http import HttpClient

log = logging.getLogger(__name__)

RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"
GOPLUS_BASE = "https://api.gopluslabs.io/api/v1"


def _f(v) -> float | None:
    try:
        if v in (None, "", "N/A"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _truthy(v) -> bool:
    return str(v).strip() in ("1", "true", "True", "yes")


@dataclass(slots=True)
class SafetyReport:
    chain: str
    address: str
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    score: float = 0.5                       # 0 = cop, 1 = temiz
    flags: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    fatal: bool = False                      # honeypot vb. -> asla alarm verme

    def add(self, penalty: float, flag: str, fatal: bool = False) -> None:
        self.score = max(0.0, self.score - penalty)
        if flag not in self.flags:
            self.flags.append(flag)
        if fatal:
            self.fatal = True

    def bonus(self, amount: float) -> None:
        self.score = min(1.0, self.score + amount)


# --------------------------------------------------------------------------- #
#  Yapisal kontroller — API gerektirmez
# --------------------------------------------------------------------------- #
def structural_check(
    rep: SafetyReport,
    *,
    liquidity_usd: float | None,
    mc_usd: float | None,
    age_minutes: float | None,
    buy_pressure: float | None,
    volume_h24: float | None,
) -> None:
    liq = liquidity_usd or 0.0
    if liq < 5_000:
        rep.add(0.30, f"likidite cok dusuk (${liq:,.0f})")
    elif liq >= 50_000:
        rep.bonus(0.10)

    # Likiditeye gore fahis piyasa degeri = cikis yok demektir.
    if liq > 0 and mc_usd and mc_usd / liq > 120:
        rep.add(0.20, f"MC/likidite orani {mc_usd / liq:.0f}x — cikis dar")

    if age_minutes is not None and age_minutes < 15:
        rep.add(0.15, "havuz 15 dakikadan yeni")
    elif age_minutes is not None and age_minutes > 1440:
        rep.bonus(0.05)

    # Hic satis yoksa cogu zaman satis engellidir (honeypot deseni).
    if buy_pressure is not None and buy_pressure > 0.97:
        rep.add(0.35, "1 saatte neredeyse hic satis yok — honeypot suphesi")

    if volume_h24 is not None and liq > 0 and volume_h24 < liq * 0.05:
        rep.add(0.10, "hacim likiditeye gore olu")


# --------------------------------------------------------------------------- #
#  RugCheck (Solana)
# --------------------------------------------------------------------------- #
async def rugcheck(http: HttpClient, mint: str, rep: SafetyReport) -> None:
    data = await http.get(f"{RUGCHECK_BASE}/tokens/{mint}/report/summary", bucket="rugcheck")
    if not isinstance(data, dict):
        return
    rep.sources.append("rugcheck")

    for risk in data.get("risks") or []:
        if not isinstance(risk, dict):
            continue
        name = str(risk.get("name") or "").strip()
        level = str(risk.get("level") or "").lower()
        if not name:
            continue
        low = name.lower()
        if "mint authority" in low or "freeze authority" in low:
            rep.add(0.30, f"rugcheck: {name}")
        elif "honeypot" in low:
            rep.add(0.60, f"rugcheck: {name}", fatal=True)
        elif level in ("danger", "high"):
            rep.add(0.20, f"rugcheck: {name}")
        elif level in ("warn", "warning", "medium"):
            rep.add(0.08, f"rugcheck: {name}")

    # score_normalised: 0 iyi, 100 kotu (RugCheck yonu tersdir).
    norm = _f(data.get("score_normalised"))
    if norm is not None:
        rep.score = min(rep.score, max(0.0, 1.0 - norm / 100.0) * 0.5 + rep.score * 0.5)


# --------------------------------------------------------------------------- #
#  GoPlus (EVM + Solana)
# --------------------------------------------------------------------------- #
async def goplus(http: HttpClient, chain: str, address: str, rep: SafetyReport) -> None:
    if chain == "solana":
        url = f"{GOPLUS_BASE}/solana/token_security"
        params = {"contract_addresses": address}
    else:
        cid = GOPLUS_CHAIN_ID.get(chain)
        if not cid:
            return
        url = f"{GOPLUS_BASE}/token_security/{cid}"
        params = {"contract_addresses": address.lower()}

    data = await http.get(url, bucket="goplus", params=params)
    result = (data or {}).get("result")
    if not isinstance(result, dict) or not result:
        return
    # Anahtar kucuk/buyuk harf farkli gelebilir.
    row = None
    for k, v in result.items():
        if str(k).lower() == address.lower():
            row = v
            break
    if row is None:
        row = next(iter(result.values()), None)
    if not isinstance(row, dict):
        return
    rep.sources.append("goplus")

    if _truthy(row.get("is_honeypot")) or _truthy(row.get("cannot_sell_all")):
        rep.add(0.70, "goplus: honeypot / satilamiyor", fatal=True)
    if _truthy(row.get("is_blacklisted")):
        rep.add(0.25, "goplus: kara liste fonksiyonu var")
    if _truthy(row.get("can_take_back_ownership")):
        rep.add(0.20, "goplus: sahiplik geri alinabilir")
    if _truthy(row.get("is_mintable")) or _truthy(row.get("mintable")):
        rep.add(0.25, "goplus: ek basim mumkun")
    if _truthy(row.get("freezeable")) or _truthy(row.get("transfer_pausable")):
        rep.add(0.20, "goplus: transfer durdurulabilir")
    if _truthy(row.get("is_open_source")) is False and chain != "solana":
        rep.add(0.15, "goplus: kaynak kod acik degil")

    for key, label in (("buy_tax", "alim"), ("sell_tax", "satim")):
        tax = _f(row.get(key))
        if tax is None:
            continue
        if tax >= 0.15:
            rep.add(0.35, f"goplus: {label} vergisi %{tax * 100:.0f}")
        elif tax >= 0.06:
            rep.add(0.12, f"goplus: {label} vergisi %{tax * 100:.0f}")

    holders = _f(row.get("holder_count"))
    if holders is not None and holders < 60:
        rep.add(0.12, f"yalnizca {holders:.0f} holder")


# --------------------------------------------------------------------------- #
#  Dis kapi
# --------------------------------------------------------------------------- #
async def check_token(
    http: HttpClient,
    chain: str,
    address: str,
    *,
    liquidity_usd: float | None = None,
    mc_usd: float | None = None,
    age_minutes: float | None = None,
    buy_pressure: float | None = None,
    volume_h24: float | None = None,
) -> SafetyReport:
    rep = SafetyReport(chain=chain, address=address)
    structural_check(
        rep,
        liquidity_usd=liquidity_usd,
        mc_usd=mc_usd,
        age_minutes=age_minutes,
        buy_pressure=buy_pressure,
        volume_h24=volume_h24,
    )
    try:
        if chain == "solana":
            await rugcheck(http, address, rep)
        await goplus(http, chain, address, rep)
    except Exception as exc:                                            # noqa: BLE001
        log.debug("guvenlik kontrolu kismen basarisiz (%s): %s", address[:10], exc)

    if not rep.sources:
        # Hicbir dis kaynak cevap vermedi. SESSIZ DUSURMUYORUZ: yapisal
        # skor gecerli kalir ama bayrak dusulur ki karta yazilsin.
        rep.flags.append("dis guvenlik kaynagi cevap vermedi")
    rep.score = max(0.0, min(1.0, rep.score))
    return rep
