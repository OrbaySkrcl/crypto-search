"""Fiyat katmaninin ortak veri tipleri."""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class Candle:
    ts: datetime          # mum baslangici, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    seconds: int = 60     # mum genisligi

    @property
    def end(self) -> datetime:
        return self.ts + timedelta(seconds=self.seconds)


@dataclass
class TokenInfo:
    chain: str
    address: str
    symbol: str | None = None
    name: str | None = None
    pair_address: str | None = None
    dex_id: str | None = None
    pair_created_at: datetime | None = None
    price_usd: float | None = None
    mc_usd: float | None = None
    fdv_usd: float | None = None
    liquidity_usd: float | None = None
    volume_24h: float | None = None
    supply_estimate: float | None = None
    txns_24h: int | None = None
    source: str = "dexscreener"

    def mc_from_price(self, price: float) -> float | None:
        """Memecoinlerde arz sabit kabul edilir; fiyattan MC turetilir."""
        if self.supply_estimate and self.supply_estimate > 0:
            return price * self.supply_estimate
        return None


@dataclass
class PriceHistory:
    """Farkli cozunurluklerden gelen mumlarin birlesik gorunumu."""

    chain: str
    address: str
    candles: list[Candle] = field(default_factory=list)
    sources: set[str] = field(default_factory=set)

    def merge(self, new: list[Candle], source: str) -> None:
        if not new:
            return
        self.sources.add(source)
        by_ts: dict[datetime, Candle] = {c.ts: c for c in self.candles}
        for c in new:
            prev = by_ts.get(c.ts)
            # Daha ince cozunurluk (kucuk `seconds`) her zaman kazanir
            if prev is None or c.seconds < prev.seconds:
                by_ts[c.ts] = c
        self.candles = sorted(by_ts.values(), key=lambda c: c.ts)

    # ------------------------------------------------------------------ #
    @property
    def start(self) -> datetime | None:
        return self.candles[0].ts if self.candles else None

    @property
    def end(self) -> datetime | None:
        return self.candles[-1].end if self.candles else None

    def _index_at(self, ts: datetime) -> int | None:
        if not self.candles:
            return None
        keys = [c.ts for c in self.candles]
        i = bisect.bisect_right(keys, ts) - 1
        return i if i >= 0 else None

    def candle_at(self, ts: datetime) -> Candle | None:
        i = self._index_at(ts)
        return self.candles[i] if i is not None else None

    def price_at(self, ts: datetime, max_gap_sec: int = 3600) -> tuple[float, float] | None:
        """(fiyat, guven) dondurur.

        Guven: mumun icine tam dusuyorsa 1.0, uzaklastikca duser. Tweet aninda
        gercek mum yoksa en yakin mumun kapanisi alinir ama guven dusurulur --
        bu deger skorlamada agirlik olarak kullanilir.
        """
        c = self.candle_at(ts)
        if c is None:
            # ts butun serinin oncesinde -> ilk mumun acilisi (dusuk guven)
            if self.candles and (self.candles[0].ts - ts).total_seconds() <= max_gap_sec:
                return (self.candles[0].open, 0.45)
            return None
        if c.ts <= ts < c.end:
            # Mum icinde: acilis-kapanis arasi lineer interpolasyon
            span = max(1.0, c.seconds)
            frac = (ts - c.ts).total_seconds() / span
            price = c.open + (c.close - c.open) * frac
            conf = 1.0 if c.seconds <= 60 else (0.9 if c.seconds <= 300 else 0.75)
            return (max(price, 1e-18), conf)
        gap = (ts - c.end).total_seconds()
        if gap > max_gap_sec:
            return None
        return (c.close, max(0.35, 1.0 - gap / max_gap_sec))

    def max_high(self, start: datetime, end: datetime) -> tuple[float, datetime] | None:
        best: tuple[float, datetime] | None = None
        for c in self.candles:
            if c.end <= start or c.ts >= end:
                continue
            if best is None or c.high > best[0]:
                best = (c.high, c.ts)
        return best

    def min_low(self, start: datetime, end: datetime) -> float | None:
        vals = [c.low for c in self.candles if c.ts < end and c.end > start and c.low > 0]
        return min(vals) if vals else None

    def close_at_or_before(self, ts: datetime) -> float | None:
        c = self.candle_at(ts)
        return c.close if c else None

    def series(self, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        return [(c.ts, c.high) for c in self.candles if c.ts < end and c.end > start]

    def max_drawdown(self, start: datetime, end: datetime) -> float | None:
        """Zirveden sonraki en buyuk dusus orani (0..1)."""
        window = [c for c in self.candles if c.ts < end and c.end > start]
        if len(window) < 2:
            return None
        peak = -1.0
        worst = 0.0
        for c in window:
            peak = max(peak, c.high)
            if peak > 0:
                worst = max(worst, (peak - c.low) / peak)
        return worst


def utc(ts: float | int) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)
