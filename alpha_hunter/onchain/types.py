"""Zincir katmaninin veri tipleri."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class BuyEvent:
    """Bir cuzdanin bir tokeni satin almasi.

    Twitter'daki "tweet"in zincir karsiligi -- ve tanim geregi ondan once gelir:
    insan once alir, sonra tweetler.
    """

    wallet: str
    token_address: str
    chain: str
    at: datetime
    usd: float | None = None
    price_usd: float | None = None
    tx: str | None = None
    source: str | None = None      # raydium / pumpfun / jupiter ...

    def key(self) -> tuple[str, str]:
        return (self.wallet, self.tx or f"{self.token_address}:{int(self.at.timestamp())}")
