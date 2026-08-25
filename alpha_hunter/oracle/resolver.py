"""TRUTH ORACLE -- bir cagrinin gercekte ne yaptigini hesaplayan katman.

Girdi:  (zincir, kontrat adresi, tweet zamani T1)
Cikti:  T1'deki fiyat/MC, T1 ONCESI tepe (copycat tespiti), T1 SONRASI tepe,
        pencere bazli getiriler, gercekci (korunan) tepe, maksimum dusus.

Fiyat kaynagi onceligi:
    1. Birdeye  (anahtar varsa: 1 dakikalik mum, genis aralik)
    2. GeckoTerminal (bedava: 1m/5m/1h karisimi)
    3. DexScreener anlik fiyat (yalnizca son care, dusuk guven)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..config import settings
from ..http import HttpClient
from ..scoring import metrics as M
from .birdeye import BirdeyeClient
from .dexscreener import DexScreenerClient
from .geckoterminal import GeckoTerminalClient
from .types import PriceHistory, TokenInfo

log = logging.getLogger(__name__)


@dataclass
class CallEvaluation:
    ok: bool = False
    reason: str | None = None

    entry_price_usd: float | None = None
    entry_mc_usd: float | None = None
    entry_liquidity_usd: float | None = None
    entry_confidence: float = 0.0
    entry_source: str | None = None

    pre_ath_mc_usd: float | None = None
    pre_ath_at: datetime | None = None
    baseline_mc_usd: float | None = None
    token_age_at_call_sec: int | None = None

    max_mc_usd_after: float | None = None
    max_mc_at: datetime | None = None
    max_multiple: float | None = None
    sustained_multiple: float | None = None
    max_drawdown_after: float | None = None
    mc_by_window: dict[str, float] = field(default_factory=dict)
    multiple_by_window: dict[str, float] = field(default_factory=dict)

    run_capture: float | None = None
    mc_earliness: float | None = None
    entry_quality: float | None = None

    global_ath_mc_usd: float | None = None
    window_closed: bool = False
    history_points: int = 0


class PriceOracle:
    def __init__(self, http: HttpClient) -> None:
        self.dex = DexScreenerClient(http)
        self.gecko = GeckoTerminalClient(http)
        self.birdeye = BirdeyeClient(http)

    # ------------------------------------------------------------------ #
    async def token_info(self, chain: str, address: str) -> TokenInfo | None:
        return await self.dex.resolve_pair_or_token(chain, address)

    async def build_history(
        self,
        chain: str,
        token: TokenInfo,
        start: datetime,
        end: datetime,
        focus: datetime | None = None,
    ) -> PriceHistory:
        """`focus` (tweet ani) etrafinda ince, uzaklarda kaba cozunurluk."""
        hist = PriceHistory(chain=chain, address=token.address)
        now = datetime.now(timezone.utc)
        end = min(end, now)
        if end <= start:
            return hist

        # ---- 1) Birdeye (anahtar varsa) ---------------------------------- #
        if self.birdeye.enabled:
            if focus:
                f0 = max(start, focus - timedelta(hours=2))
                f1 = min(end, focus + timedelta(hours=26))
                hist.merge(await self.birdeye.ohlcv(chain, token.address, f0, f1, "1m"), "birdeye")
            hist.merge(await self.birdeye.ohlcv(chain, token.address, start, end, "15m"), "birdeye")
            if (end - start) > timedelta(days=10):
                hist.merge(await self.birdeye.ohlcv(chain, token.address, start, end, "1h"), "birdeye")
            if hist.candles:
                return hist

        # ---- 2) GeckoTerminal (bedava) ----------------------------------- #
        pool = token.pair_address or await self.gecko.find_pool(chain, token.address)
        if not pool:
            return hist

        # Kaba: butun araligi saatlik mumlarla kapla
        hist.merge(await self.gecko.range(chain, pool, start, end, "1h"), "geckoterminal")
        # Orta: ilk 3 gun 5 dakikalik
        if focus:
            m0 = max(start, focus - timedelta(hours=6))
            m1 = min(end, focus + timedelta(hours=76))
            hist.merge(await self.gecko.range(chain, pool, m0, m1, "5m"), "geckoterminal")
            # Ince: tweet aninin +-8 saati 1 dakikalik  -> giris fiyati burada
            f0 = max(start, focus - timedelta(hours=2))
            f1 = min(end, focus + timedelta(hours=14))
            hist.merge(await self.gecko.range(chain, pool, f0, f1, "1m"), "geckoterminal")
        return hist

    # ------------------------------------------------------------------ #
    async def evaluate(
        self,
        chain: str,
        address: str,
        called_at: datetime,
        token: TokenInfo | None = None,
        history: PriceHistory | None = None,
    ) -> CallEvaluation:
        ev = CallEvaluation()
        token = token or await self.token_info(chain, address)
        if token is None or not token.address:
            ev.reason = "token DEX'te bulunamadi"
            return ev

        now = datetime.now(timezone.utc)
        close_at = called_at + timedelta(hours=settings.eval_close_after_hours)
        ev.window_closed = now >= close_at

        birth = token.pair_created_at or (called_at - timedelta(days=30))
        hist_start = min(birth, called_at - timedelta(hours=6))
        hist_end = min(now, close_at)

        hist = history or await self.build_history(chain, token, hist_start, hist_end, focus=called_at)
        ev.history_points = len(hist.candles)

        supply = token.supply_estimate
        def to_mc(price: float | None) -> float | None:
            if price is None:
                return None
            return price * supply if supply and supply > 0 else None

        # ---------------- KURAL 1: T1 anindaki fiyat --------------------- #
        at = hist.price_at(called_at)
        if at:
            ev.entry_price_usd, ev.entry_confidence = at
            ev.entry_source = "|".join(sorted(hist.sources)) or "ohlcv"
        elif self.birdeye.enabled:
            p = await self.birdeye.price_at(chain, token.address, called_at)
            if p:
                ev.entry_price_usd, ev.entry_confidence, ev.entry_source = p, 0.85, "birdeye_point"
        if ev.entry_price_usd is None:
            # Son care: cagri cok yeniyse anlik fiyat makul bir yaklasimdir
            age = (now - called_at).total_seconds()
            if token.price_usd and age <= 900:
                ev.entry_price_usd = token.price_usd
                ev.entry_confidence = 0.30
                ev.entry_source = "dexscreener_live"
            else:
                ev.reason = "T1 icin fiyat verisi yok"
                return ev

        ev.entry_mc_usd = to_mc(ev.entry_price_usd)
        ev.entry_liquidity_usd = token.liquidity_usd
        ev.token_age_at_call_sec = (
            int((called_at - token.pair_created_at).total_seconds())
            if token.pair_created_at else None
        )

        # -------------- Tweet ONCESI tepe (copycat sinyali) --------------- #
        pre = hist.max_high(hist_start, called_at)
        if pre:
            ev.pre_ath_mc_usd = to_mc(pre[0])
            ev.pre_ath_at = pre[1]
        base_low = hist.min_low(hist_start, called_at) or hist.min_low(hist_start, hist_end)
        ev.baseline_mc_usd = to_mc(base_low) if base_low else None

        # -------------- Tweet SONRASI hareket ----------------------------- #
        post_end = hist_end
        post = hist.max_high(called_at, post_end)
        max_price_after = post[0] if post else ev.entry_price_usd
        if post:
            ev.max_mc_at = post[1]
        ev.max_mc_usd_after = to_mc(max_price_after)
        ev.max_multiple = (max_price_after / ev.entry_price_usd) if ev.entry_price_usd else None
        ev.max_drawdown_after = hist.max_drawdown(called_at, post_end)

        # Gercekci tepe: en az N dakika korunan seviye (ATH kagit uzerindedir)
        series = hist.series(called_at, post_end)
        sustained = M.sustained_high(series, settings.sustained_high_minutes)
        if sustained and ev.entry_price_usd:
            ev.sustained_multiple = max(sustained / ev.entry_price_usd, 0.0)
        else:
            ev.sustained_multiple = ev.max_multiple

        # -------------- Pencere bazli getiriler --------------------------- #
        for h in settings.eval_window_list:
            t = called_at + timedelta(hours=h)
            if t > now:
                continue
            px = hist.close_at_or_before(t)
            if px is None:
                continue
            ev.mc_by_window[str(h)] = to_mc(px) or px
            if ev.entry_price_usd:
                ev.multiple_by_window[str(h)] = px / ev.entry_price_usd

        # -------------- Turetilmis erkencilik metrikleri ------------------ #
        ev.global_ath_mc_usd = max(
            [v for v in (ev.pre_ath_mc_usd, ev.max_mc_usd_after) if v is not None], default=None
        )
        ev.mc_earliness = M.mc_earliness(
            ev.entry_mc_usd, settings.mc_earliness_low_usd, settings.mc_earliness_high_usd
        ) if ev.entry_mc_usd else None
        ev.run_capture = M.run_capture(
            ev.entry_mc_usd, ev.max_mc_usd_after, ev.baseline_mc_usd, ev.global_ath_mc_usd
        )
        # MC bilinmiyorsa (arz cozulemedi) saf fiyat oranlariyla ayni sonuc cikar
        if ev.run_capture is None and ev.entry_price_usd:
            base_p = base_low
            ath_p = max(
                [x for x in (pre[0] if pre else None, max_price_after) if x is not None], default=None
            )
            ev.run_capture = M.run_capture(ev.entry_price_usd, max_price_after, base_p, ath_p)
        ev.entry_quality = M.entry_quality(ev.mc_earliness, ev.run_capture)

        ev.ok = True
        return ev
