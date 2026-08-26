"""Calisma zamani ayarlari.

Yalnizca UC deger degistirilebilir. Bilerek: her esigi ayarlanabilir
yapmak kullaniciyi ayar menusunde bogar ve kimse bir daha dokunmaz.
Degisen degerler veritabaninda yasar, yeniden baslatmayi atlatir.
"""
from __future__ import annotations

from ..config import settings
from ..db import get_state, set_state

# anahtar -> (env varsayilani, tur, alt sinir, ust sinir, aciklama)
FIELDS: dict[str, tuple] = {
    "esik_cuzdan": (
        lambda: settings.confluence_min_wallets, int, 2, 8,
        "Konfluans icin gereken bagimsiz akilli cuzdan sayisi",
    ),
    "esik_likidite": (
        lambda: settings.alert_min_liquidity_usd, float, 1_000, 500_000,
        "Alarm icin minimum likidite (USD)",
    ),
    "esik_fiyat": (
        lambda: settings.watch_price_pct, float, 3, 300,
        "Takip listesi fiyat hareketi esigi (%)",
    ),
}


def get(name: str):
    if name not in FIELDS:
        raise KeyError(name)
    default_fn, cast, lo, hi, _desc = FIELDS[name]
    raw = get_state(f"cfg:{name}")
    if raw is None:
        return default_fn()
    try:
        return max(lo, min(hi, cast(raw)))
    except (TypeError, ValueError):
        return default_fn()


def set_value(name: str, raw: str) -> tuple[bool, str]:
    if name not in FIELDS:
        return False, f"bilinmeyen ayar: {name}"
    _default_fn, cast, lo, hi, _desc = FIELDS[name]
    try:
        val = cast(str(raw).replace(",", ".").strip())
    except (TypeError, ValueError):
        return False, f"'{raw}' sayi degil"
    if not (lo <= val <= hi):
        return False, f"deger {lo:g} ile {hi:g} arasinda olmali"
    set_state(f"cfg:{name}", str(val))
    return True, f"{name} = {val:g}"


def all_values() -> list[tuple[str, object, str]]:
    return [(k, get(k), FIELDS[k][4]) for k in FIELDS]
