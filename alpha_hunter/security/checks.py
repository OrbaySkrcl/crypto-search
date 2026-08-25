"""ANTI-SCAM KATMANI.

Bir hesabin "10x call" yapmis gorunmesi yetmez -- o coin honeypot/rug ise
o cagri basari degil, kurbandir. Bu katman token'in yapisal riskini olcer ve
skorlamada `survivorship` bacagini besler.

Kontroller (Solana):
  * mint authority acik mi   -> sonsuz basim riski
  * freeze authority acik mi -> cuzdan dondurma riski
  * ilk 10 cuzdan yogunlugu  -> bundle/sniper riski
  * likidite dusus orani     -> rug tespiti (zaman icinde)
  * RugCheck.xyz raporu      -> ucuncu taraf ikinci goruş
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import settings
from ..http import HttpClient

log = logging.getLogger(__name__)


@dataclass
class SecurityReport:
    chain: str
    address: str
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    mint_authority: str | None = None
    freeze_authority: str | None = None
    mint_renounced: bool | None = None
    freeze_renounced: bool | None = None
    supply: float | None = None
    decimals: int | None = None
    top10_pct: float | None = None
    holder_sample: int | None = None

    rugcheck_score: float | None = None
    rugcheck_risks: list[str] = field(default_factory=list)

    score: float = 0.5           # 0 = cop, 1 = temiz
    flags: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "mint_authority": self.mint_authority,
            "freeze_authority": self.freeze_authority,
            "mint_renounced": self.mint_renounced,
            "freeze_renounced": self.freeze_renounced,
            "supply": self.supply,
            "decimals": self.decimals,
            "top10_pct": self.top10_pct,
            "holder_sample": self.holder_sample,
            "rugcheck_score": self.rugcheck_score,
            "rugcheck_risks": self.rugcheck_risks,
            "flags": self.flags,
            "sources": self.sources,
            "checked_at": self.checked_at.isoformat(),
        }


class SecurityChecker:
    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self.rpc = (
            f"https://mainnet.helius-rpc.com/?api-key={settings.helius_api_key}"
            if settings.helius_api_key
            else settings.solana_rpc_url
        )

    # ------------------------------------------------------------------ #
    async def _rpc(self, method: str, params: list) -> dict | None:
        data = await self.http.post(
            self.rpc,
            bucket="rpc",
            json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={"Content-Type": "application/json"},
            max_retries=2,
        )
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return None

    async def solana_mint_info(self, mint: str, rep: SecurityReport) -> None:
        res = await self._rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        info = (((res or {}).get("value") or {}).get("data") or {}).get("parsed", {}).get("info")
        if not isinstance(info, dict):
            return
        rep.sources.append("solana_rpc")
        rep.mint_authority = info.get("mintAuthority")
        rep.freeze_authority = info.get("freezeAuthority")
        rep.mint_renounced = rep.mint_authority in (None, "")
        rep.freeze_renounced = rep.freeze_authority in (None, "")
        rep.decimals = info.get("decimals")
        try:
            raw = float(info.get("supply") or 0)
            rep.supply = raw / (10 ** int(rep.decimals or 0))
        except (TypeError, ValueError):
            pass

    async def solana_holders(self, mint: str, rep: SecurityReport) -> None:
        res = await self._rpc("getTokenLargestAccounts", [mint])
        vals = (res or {}).get("value") or []
        if not vals:
            return
        amounts: list[float] = []
        for v in vals:
            try:
                amounts.append(float(v.get("uiAmount") or 0))
            except (TypeError, ValueError):
                continue
        if not amounts or not rep.supply or rep.supply <= 0:
            return
        amounts.sort(reverse=True)
        rep.holder_sample = len(amounts)
        # NOT: ilk sirada genelde LP havuzu olur; onu haric tutmak icin
        # supply'in %35'inden buyuk tek cuzdani havuz kabul ediyoruz.
        filtered = [a for a in amounts if a / rep.supply < 0.35] or amounts[1:]
        rep.top10_pct = min(1.0, sum(filtered[:10]) / rep.supply)

    async def rugcheck(self, mint: str, rep: SecurityReport) -> None:
        data = await self.http.get(
            f"{settings.rugcheck_base.rstrip('/')}/tokens/{mint}/report/summary",
            bucket="rugcheck",
            max_retries=1,
        )
        if not isinstance(data, dict):
            return
        rep.sources.append("rugcheck")
        raw = data.get("score_normalised", data.get("score"))
        try:
            rep.rugcheck_score = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            rep.rugcheck_score = None
        for r in (data.get("risks") or []):
            if isinstance(r, dict) and r.get("name"):
                lvl = r.get("level", "")
                rep.rugcheck_risks.append(f"{r['name']}" + (f" ({lvl})" if lvl else ""))

    # ------------------------------------------------------------------ #
    async def check(self, chain: str, address: str, use_rugcheck: bool = True) -> SecurityReport:
        rep = SecurityReport(chain=chain, address=address)
        if chain == "solana":
            await self.solana_mint_info(address, rep)
            if rep.supply:
                await self.solana_holders(address, rep)
            if use_rugcheck:
                await self.rugcheck(address, rep)
        rep.score = compute_security_score(rep)
        return rep


def compute_security_score(rep: SecurityReport) -> float:
    """0 (cop) .. 1 (temiz). Bilinmeyenler notr 0.5'ten baslar."""
    score = 0.65
    if rep.mint_renounced is True:
        score += 0.12
    elif rep.mint_renounced is False:
        score -= 0.30
        rep.flags.append("mint_authority_aktif")

    if rep.freeze_renounced is True:
        score += 0.08
    elif rep.freeze_renounced is False:
        score -= 0.25
        rep.flags.append("freeze_authority_aktif")

    if rep.top10_pct is not None:
        if rep.top10_pct > 0.60:
            score -= 0.30
            rep.flags.append(f"ilk10_yogunluk_%{rep.top10_pct*100:.0f}")
        elif rep.top10_pct > 0.35:
            score -= 0.12
            rep.flags.append(f"ilk10_yogunluk_%{rep.top10_pct*100:.0f}")
        else:
            score += 0.06

    if rep.rugcheck_score is not None:
        # RugCheck'te yuksek skor = yuksek RISK
        if rep.rugcheck_score >= 60:
            score -= 0.25
            rep.flags.append("rugcheck_yuksek_risk")
        elif rep.rugcheck_score <= 20:
            score += 0.08
    danger = {"danger", "critical"}
    if any(any(d in r.lower() for d in danger) for r in rep.rugcheck_risks):
        score -= 0.15
        rep.flags.append("rugcheck_danger_bayragi")

    return max(0.0, min(1.0, score))


def detect_rug(
    last_liquidity_usd: float | None, peak_liquidity_usd: float | None
) -> tuple[bool, str | None]:
    """Likidite tabanli rug tespiti. Fiyat degil LIKIDITE bakilir --
    fiyat sifira gitmeden once likidite cekilir."""
    if last_liquidity_usd is None:
        return (False, None)
    if last_liquidity_usd < settings.rug_liquidity_floor_usd:
        return (True, f"likidite ${last_liquidity_usd:,.0f} tabanin altinda")
    if peak_liquidity_usd and peak_liquidity_usd > 0:
        drop = 1.0 - (last_liquidity_usd / peak_liquidity_usd)
        if drop >= settings.rug_liquidity_drop_pct:
            return (True, f"likidite zirveden %{drop*100:.0f} dustu")
    return (False, None)
