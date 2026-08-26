"""ADIM 4 — CUZDAN SICILI: kadro elle secilmez, HAK EDILIR.

Referans urunlerin "elle secilmis shark kadrosu" pazarlamadir; kimse o
kadronun neye gore secildigini gosteremez. Burada kadro olculur:

  1. Her alim, alimdan SONRAKI korunmus tepeye gore puanlanir.
  2. Isabet orani ham degil Wilson alt siniriyla alinir (3/3 = %31).
  3. Buyukluk ortalamayla degil MEDYANLA olculur (tek 100x tabloyu bozmasin).
  4. Giris piyasa degeri dusukse odul, zirvede alim varsa ceza.
  5. Sprey/dust/altyapi cuzdanlari kadroya hic girmez.

Cuzdan "akilli" olur, secilmez. Sicili bozulursa kadrodan duser.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import Token, Trade, Wallet, session_scope, utcnow
from . import repo
from .stats import inverse_log_scale, log_scale, median, wilson_lower_bound

log = logging.getLogger(__name__)

# Skor agirliklari — toplami 1.0
W_RELIABILITY = 0.45      # tutturuyor mu (Wilson)
W_MAGNITUDE = 0.35        # ne kadar tutturuyor (medyan kat)
W_EARLINESS = 0.20        # ne kadar erken giriyor (medyan giris MC)

# Giris kalitesi olcegi: 20k'da tam puan, 5M'de sifir
EARLY_MC_LOW = 20_000.0
EARLY_MC_HIGH = 5_000_000.0
# Buyukluk olcegi: 1x sifir, 10x tam puan
MAG_LOW = 1.0
MAG_HIGH = 10.0
# Yaygınlik filtresi bu kadar token gorulmeden devreye girmez.
UBIQUITY_MIN_UNIVERSE = 50


# --------------------------------------------------------------------------- #
#  1) Alimlari sonuclandir
# --------------------------------------------------------------------------- #
def evaluate_trades(session: Session, limit: int = 3_000) -> int:
    """Suresi dolmus alimlara sonuc carpani yazar."""
    horizon = utcnow() - timedelta(hours=settings.wallet_eval_hours)
    rows = list(
        session.scalars(
            select(Trade)
            .where(
                Trade.side == "buy",
                Trade.result_multiple.is_(None),
                Trade.at <= horizon,
            )
            .order_by(Trade.at.asc())
            .limit(limit)
        )
    )
    done = 0
    for tr in rows:
        entry = tr.mc_usd or repo.price_at(session, tr.token_id, tr.at)
        if not entry or entry <= 0:
            # Giris fiyati bilinmiyorsa bu alim OLCULEMEZ. Sifir yazmak
            # cuzdana haksizlik olur; 1.0 yazmak istatistigi sulandirir.
            # Isaretleyip bir daha bakmiyoruz.
            tr.result_multiple = 0.0
            tr.evaluated_at = utcnow()
            continue
        peak, _at = repo.sustained_peak(
            session,
            tr.token_id,
            since=tr.at,
            until=tr.at + timedelta(hours=settings.wallet_eval_hours),
        )
        if peak is None:
            tok = session.get(Token, tr.token_id)
            peak = (tok.last_mc_usd if tok else None) or entry
        tr.result_multiple = max(0.0, peak / entry)
        tr.evaluated_at = utcnow()
        done += 1
    return done


# --------------------------------------------------------------------------- #
#  2) Cuzdanlari puanla
# --------------------------------------------------------------------------- #
def _block_reason(
    *, n_tokens: int, active_days: float, avg_buy: float | None,
    ubiquity: float, universe: int,
) -> str | None:
    if active_days > 0 and n_tokens / active_days > settings.wallet_max_tokens_per_day:
        return f"sprey ({n_tokens / active_days:.0f} token/gun)"
    if avg_buy is not None and avg_buy < settings.wallet_min_avg_buy_usd:
        return f"dust (ort. ${avg_buy:,.0f})"
    # Yaygınlik filtresi ancak evren buyudugunde anlamlidir: 10 tokenle
    # calisirken "tokenlerin %30'unda" olmak siradan bir durumdur.
    if universe >= UBIQUITY_MIN_UNIVERSE and ubiquity > settings.wallet_ubiquity_max_ratio:
        return f"altyapi/bot (tokenlerin %{ubiquity * 100:.0f}'inde)"
    return None


def score_wallets(session: Session) -> dict:
    """Tum cuzdanlari yeniden puanlar."""
    window_start = utcnow() - timedelta(days=settings.wallet_score_window_days)
    total_tokens = session.scalar(select(func.count(Token.id))) or 1

    rows = list(
        session.execute(
            select(
                Trade.wallet_id, Trade.token_id, Trade.side, Trade.at,
                Trade.usd, Trade.mc_usd, Trade.result_multiple,
            ).where(Trade.at >= window_start)
        )
    )
    if not rows:
        return {"cuzdan": 0, "akilli": 0}

    per_wallet: dict[int, list] = defaultdict(list)
    for r in rows:
        per_wallet[r.wallet_id].append(r)

    n_smart = 0
    for wallet_id, trades in per_wallet.items():
        w = session.get(Wallet, wallet_id)
        if w is None:
            continue

        buys = [t for t in trades if t.side == "buy"]
        sells = [t for t in trades if t.side == "sell"]
        tokens = {t.token_id for t in trades}
        times = [t.at for t in trades]
        span_days = max(1.0, (max(times) - min(times)).total_seconds() / 86400) if times else 1.0

        buy_usd = [t.usd for t in buys if t.usd]
        avg_buy = sum(buy_usd) / len(buy_usd) if buy_usd else None
        ubiquity = len(tokens) / max(1, total_tokens)

        w.n_tokens = len(tokens)
        w.n_buys = len(buys)
        w.n_sells = len(sells)
        w.avg_buy_usd = avg_buy
        w.scored_at = utcnow()

        reason = _block_reason(
            n_tokens=len(tokens), active_days=span_days, avg_buy=avg_buy,
            ubiquity=ubiquity, universe=total_tokens,
        )
        if reason:
            w.blocked = True
            w.block_reason = reason
            w.smart = False
            w.score = 0.0
            continue
        w.blocked = False
        w.block_reason = None

        # Yalnizca sonuclanmis ve olculebilir alimlar
        graded = [t for t in buys if t.result_multiple and t.result_multiple > 0]
        w.n_evaluated = len(graded)
        if len(graded) < settings.wallet_min_evaluated:
            w.smart = False
            w.score = 0.0
            w.win_rate = None
            w.wilson = None
            w.median_multiple = None
            continue

        mults = [t.result_multiple for t in graded]
        wins = sum(1 for m in mults if m >= settings.wallet_win_multiple)
        w.n_wins = wins
        w.win_rate = wins / len(graded)
        w.wilson = wilson_lower_bound(wins, len(graded))
        w.median_multiple = median(mults)
        entry_mcs = [t.mc_usd for t in graded if t.mc_usd]
        w.median_entry_mc = median(entry_mcs) if entry_mcs else None

        score = (
            W_RELIABILITY * w.wilson
            + W_MAGNITUDE * log_scale(w.median_multiple, MAG_LOW, MAG_HIGH)
            + W_EARLINESS * inverse_log_scale(w.median_entry_mc, EARLY_MC_LOW, EARLY_MC_HIGH)
        ) * 100.0
        w.score = round(score, 1)
        w.smart = score >= settings.wallet_smart_score_min
        if w.smart:
            n_smart += 1

    return {"cuzdan": len(per_wallet), "akilli": n_smart}


def run_scoring() -> dict:
    with session_scope() as s:
        n_eval = evaluate_trades(s)
    with session_scope() as s:
        out = score_wallets(s)
    out["sonuclanan_alim"] = n_eval
    return out


# --------------------------------------------------------------------------- #
#  Okuma
# --------------------------------------------------------------------------- #
def smart_wallets(session: Session, limit: int = 15) -> list[dict]:
    rows = list(
        session.scalars(
            select(Wallet)
            .where(Wallet.smart.is_(True), Wallet.blocked.is_(False))
            .order_by(Wallet.score.desc())
            .limit(limit)
        )
    )
    return [
        {
            "adres": w.address,
            "kisa": w.short(),
            "zincir": w.chain,
            "skor": w.score,
            "isabet": w.win_rate,
            "wilson": w.wilson,
            "n": w.n_evaluated,
            "kazanan": w.n_wins,
            "medyan_kat": w.median_multiple,
            "medyan_giris_mc": w.median_entry_mc,
            "token": w.n_tokens,
            "son_gorulme": w.last_seen_at,
        }
        for w in rows
    ]
