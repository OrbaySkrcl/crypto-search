"""Radar cekirdegi — uctan uca senaryo, agsiz.

Kurulan hikaye:
  * SNIPER_A/B/C : 8 tokeni erken alan, medyan 5x yapan cuzdanlar
  * SPRAY        : 5 gunde 200 tokene dokunan tarama botu
  * DUST         : 12 dolarlik alimlar yapan gurultu cuzdani
  * NEWCOIN      : uc sniper'in ayni pencerede aldigi yeni token

Beklenen: uc sniper kadroya girer, SPRAY ve DUST elenir, NEWCOIN icin
KONFLUANS karti uretilir, kart karneye islenir ve olculur.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from radar.core import journal, repo, signals, wallets
from radar.core.stats import median, percentile, tradeable_usd, wilson_lower_bound
from radar.db import Alert, AlertKind, Outcome, PriceSnapshot, Token, Trade, Wallet, session_scope, utcnow

CHAIN = "solana"


# --------------------------------------------------------------------------- #
#  Kurulum yardimcilari
# --------------------------------------------------------------------------- #
def make_token(s, symbol: str, *, mc: float, liq: float = 90_000.0,
               age_hours: float = 72.0, vol_h1: float = 30_000.0,
               vol_h24: float = 300_000.0) -> Token:
    tok, _ = repo.upsert_token(
        s, CHAIN, f"MINT{symbol}", symbol=symbol, pair_address=f"PAIR{symbol}",
        dex_id="raydium", price_usd=mc / 1e9, mc_usd=mc, liquidity_usd=liq,
        volume_h1=vol_h1, volume_h24=vol_h24,
        pool_created_at=utcnow() - timedelta(hours=age_hours),
    )
    return tok


def add_snapshots(s, token: Token, start, series: list[tuple[float, float]]) -> None:
    """series = [(dakika_ofseti, mc), ...]"""
    for minutes, mc in series:
        s.add(
            PriceSnapshot(
                token_id=token.id, at=start + timedelta(minutes=minutes),
                price_usd=mc / 1e9, mc_usd=mc, liquidity_usd=token.last_liquidity_usd,
                volume_h1=token.last_volume_h1,
            )
        )


def add_buy(s, token: Token, wallet: Wallet, *, at, usd: float, mc: float) -> Trade:
    tr, _ = repo.record_trade(
        s, token, wallet, side="buy", at=at, usd=usd,
        price_usd=mc / 1e9, tx_hash=f"tx-{wallet.address}-{token.symbol}-{at.timestamp():.0f}",
    )
    tr.mc_usd = mc
    return tr


def seed_history(s, wallet_names: list[str], *, n_tokens: int = 8,
                 entry_mc: float = 30_000.0, peak_mult: float = 5.0,
                 buy_usd: float = 900.0) -> list[Wallet]:
    """Gecmiste kosmus tokenler ve onlari erken alan cuzdanlar."""
    ws = [repo.upsert_wallet(s, CHAIN, name) for name in wallet_names]
    for i in range(n_tokens):
        days_ago = 20 - i
        bought_at = utcnow() - timedelta(days=days_ago)
        tok = make_token(s, f"OLD{i}", mc=entry_mc * peak_mult, age_hours=days_ago * 24)
        # Alimdan sonra fiyat egrisi: giris -> tepe -> tepe korunuyor
        add_snapshots(s, tok, bought_at, [
            (0, entry_mc), (15, entry_mc * 1.8), (30, entry_mc * 3.0),
            (45, entry_mc * peak_mult), (60, entry_mc * peak_mult),
            (75, entry_mc * peak_mult * 0.99), (120, entry_mc * peak_mult * 0.8),
            (360, entry_mc * peak_mult * 0.6),
        ])
        for w in ws:
            add_buy(s, tok, w, at=bought_at, usd=buy_usd, mc=entry_mc)
    return ws


# --------------------------------------------------------------------------- #
#  Istatistik temelleri
# --------------------------------------------------------------------------- #
def test_wilson_punishes_small_samples():
    assert wilson_lower_bound(3, 3) == pytest.approx(0.44, abs=0.01)
    assert wilson_lower_bound(30, 40) == pytest.approx(0.60, abs=0.01)
    # 3/3 (ham %100), 30/40'tan (ham %75) DAHA DUSUK puanlanir
    assert wilson_lower_bound(3, 3) < wilson_lower_bound(30, 40)
    assert wilson_lower_bound(0, 0) == 0.0


def test_median_ignores_single_outlier():
    # Bir tane 100x, kirk tane cop: ortalama 3x der, medyan gercegi soyler
    assert median([1.0, 1.1, 0.9, 100.0]) == pytest.approx(1.05)
    assert median([]) is None
    assert percentile([1, 2, 3, 4], 0.5) == pytest.approx(2.5)


def test_tradeable_usd_exposes_paper_gains():
    """2 bin dolarlik havuzdaki 50x kagit uzerindedir."""
    assert tradeable_usd(2_000) == pytest.approx(100)
    assert tradeable_usd(500_000) == pytest.approx(25_000)
    assert tradeable_usd(None) == 0.0


def test_sustained_peak_rejects_wick(rdb):
    """30 saniye gorulen tepeden cikamazsin: fitil elenmeli."""
    with session_scope() as s:
        tok = make_token(s, "WICK", mc=100_000)
        start = utcnow() - timedelta(hours=6)
        add_snapshots(s, tok, start, [
            (0, 100_000),
            (5, 900_000),        # fitil: bir sonraki ornekte yok oluyor
            (10, 120_000),
            (25, 250_000),       # bu seviye korunuyor
            (40, 250_000),
            (55, 240_000),
        ])
        s.flush()
        peak, _at = repo.sustained_peak(s, tok.id, since=start)

    assert peak == pytest.approx(250_000)     # 900k degil


# --------------------------------------------------------------------------- #
#  Cuzdan skorlama
# --------------------------------------------------------------------------- #
def test_good_wallets_become_smart_bots_get_blocked(rdb):
    with session_scope() as s:
        seed_history(s, ["SNIPER_A", "SNIPER_B", "SNIPER_C"])

        # SPRAY: 5 gunde 60 farkli token -> 12/gun esigini asar
        spray = repo.upsert_wallet(s, CHAIN, "SPRAY")
        for i in range(60):
            tok = make_token(s, f"SPR{i}", mc=50_000)
            add_buy(s, tok, spray, at=utcnow() - timedelta(days=5 - i / 20),
                    usd=800, mc=50_000)

        # DUST: 12 dolarlik alimlar
        dust = repo.upsert_wallet(s, CHAIN, "DUST")
        for i in range(6):
            tok = make_token(s, f"DST{i}", mc=50_000)
            add_buy(s, tok, dust, at=utcnow() - timedelta(days=6, hours=i),
                    usd=12, mc=50_000)

    out = wallets.run_scoring()
    assert out["sonuclanan_alim"] > 0

    with session_scope() as s:
        smart = {w.address for w in s.scalars(select(Wallet).where(Wallet.smart.is_(True)))}
        assert smart == {"SNIPER_A", "SNIPER_B", "SNIPER_C"}

        spray = s.scalar(select(Wallet).where(Wallet.address == "SPRAY"))
        assert spray.blocked and "sprey" in spray.block_reason

        dust = s.scalar(select(Wallet).where(Wallet.address == "DUST"))
        assert dust.blocked and "dust" in dust.block_reason

        a = s.scalar(select(Wallet).where(Wallet.address == "SNIPER_A"))
        assert a.n_evaluated == 8 and a.n_wins == 8
        assert a.median_multiple == pytest.approx(5.0, rel=0.05)
        assert a.wilson == pytest.approx(0.68, abs=0.01)   # 8/8 ham %100 degil


def test_wallet_needs_minimum_track_record(rdb):
    """Iki isabetli alim kadroya girmeye yetmez."""
    with session_scope() as s:
        seed_history(s, ["LUCKY"], n_tokens=2)
    wallets.run_scoring()
    with session_scope() as s:
        w = s.scalar(select(Wallet).where(Wallet.address == "LUCKY"))
        assert w.smart is False
        assert w.n_evaluated == 2


# --------------------------------------------------------------------------- #
#  Konfluans
# --------------------------------------------------------------------------- #
def _prepare_smart_trio(s) -> list[Wallet]:
    return seed_history(s, ["SNIPER_A", "SNIPER_B", "SNIPER_C"])


@pytest.mark.asyncio
async def test_confluence_fires_and_card_carries_numbers(rdb, fake_http):
    from radar.bot import cards

    with session_scope() as s:
        _prepare_smart_trio(s)
    wallets.run_scoring()

    with session_scope() as s:
        new = make_token(s, "NEWCOIN", mc=400_000, liq=120_000, age_hours=5)
        now = utcnow()
        for i, name in enumerate(("SNIPER_A", "SNIPER_B", "SNIPER_C")):
            w = repo.upsert_wallet(s, CHAIN, name)
            add_buy(s, new, w, at=now - timedelta(hours=i + 1), usd=2_500, mc=380_000)
        token_id = new.id

    # Guvenlik kaynaklari temiz cevap versin
    fake_http.add("/report/summary", {"score_normalised": 5, "risks": []})
    created = await signals.scan(fake_http)

    assert len(created) == 1
    with session_scope() as s:
        al = s.get(Alert, created[0])
        assert al.kind == AlertKind.CONFLUENCE.value
        assert al.token_id == token_id
        assert al.entry_mc_usd == pytest.approx(400_000)
        p = al.data()
        assert p["cuzdan_sayisi"] == 3
        assert p["satan_cuzdan"] == 0
        assert p["toplam_usd"] == pytest.approx(7_500)
        assert len(p["cuzdanlar"]) == 3

        tok = s.get(Token, token_id)
        text, keyboard = cards.render_alert(s, al, tok, watched=False)

    assert "KONFLUANS" in text and "NEWCOIN" in text
    assert "3</b> bagimsiz akilli cuzdan" in text
    assert "girilebilir" in text          # kagit uzerinde kat uyarisi
    assert "Yatirim tavsiyesi degildir" in text
    assert any(b["callback_data"] == f"wa:{token_id}" for row in keyboard for b in row)


@pytest.mark.asyncio
async def test_two_wallets_is_not_confluence(rdb, fake_http):
    with session_scope() as s:
        _prepare_smart_trio(s)
    wallets.run_scoring()

    with session_scope() as s:
        new = make_token(s, "TWOONLY", mc=400_000, liq=120_000, age_hours=5)
        for i, name in enumerate(("SNIPER_A", "SNIPER_B")):
            w = repo.upsert_wallet(s, CHAIN, name)
            add_buy(s, new, w, at=utcnow() - timedelta(hours=i + 1), usd=2_000, mc=380_000)

    fake_http.add("/report/summary", {"score_normalised": 5, "risks": []})
    created = await signals.scan(fake_http)
    kinds = []
    with session_scope() as s:
        for aid in created:
            kinds.append(s.get(Alert, aid).kind)
    assert AlertKind.CONFLUENCE.value not in kinds


@pytest.mark.asyncio
async def test_confluence_is_deduplicated(rdb, fake_http):
    with session_scope() as s:
        _prepare_smart_trio(s)
    wallets.run_scoring()
    with session_scope() as s:
        new = make_token(s, "DEDUPE", mc=400_000, liq=120_000, age_hours=5)
        for i, name in enumerate(("SNIPER_A", "SNIPER_B", "SNIPER_C")):
            w = repo.upsert_wallet(s, CHAIN, name)
            add_buy(s, new, w, at=utcnow() - timedelta(hours=i + 1), usd=1_000, mc=380_000)

    fake_http.add("/report/summary", {"score_normalised": 5, "risks": []})
    first = await signals.scan(fake_http)
    second = await signals.scan(fake_http)
    assert len(first) == 1
    assert second == []


@pytest.mark.asyncio
async def test_safety_gate_blocks_honeypot(rdb, fake_http):
    with session_scope() as s:
        _prepare_smart_trio(s)
    wallets.run_scoring()
    with session_scope() as s:
        bad = make_token(s, "TRAP", mc=400_000, liq=120_000, age_hours=5)
        for i, name in enumerate(("SNIPER_A", "SNIPER_B", "SNIPER_C")):
            w = repo.upsert_wallet(s, CHAIN, name)
            add_buy(s, bad, w, at=utcnow() - timedelta(hours=i + 1), usd=1_000, mc=380_000)

    fake_http.add("/report/summary", {
        "score_normalised": 95,
        "risks": [{"name": "Honeypot detected", "level": "danger"}],
    })
    created = await signals.scan(fake_http)
    assert created == []


@pytest.mark.asyncio
async def test_thin_liquidity_never_alerts(rdb, fake_http):
    with session_scope() as s:
        _prepare_smart_trio(s)
    wallets.run_scoring()
    with session_scope() as s:
        # 3.000 dolar likidite: burada gorulen kat girilebilir degil
        thin = make_token(s, "THIN", mc=400_000, liq=3_000, age_hours=5)
        for i, name in enumerate(("SNIPER_A", "SNIPER_B", "SNIPER_C")):
            w = repo.upsert_wallet(s, CHAIN, name)
            add_buy(s, thin, w, at=utcnow() - timedelta(hours=i + 1), usd=1_000, mc=380_000)

    fake_http.add("/report/summary", {"score_normalised": 5, "risks": []})
    assert await signals.scan(fake_http) == []


# --------------------------------------------------------------------------- #
#  Hacim
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_volume_spike_detected(rdb, fake_http):
    with session_scope() as s:
        # 1 saatte 200k, 24 saatte 300k -> saatlik ortalama 12.5k, kat ~16
        make_token(s, "PUMP", mc=800_000, liq=150_000, age_hours=30,
                   vol_h1=200_000, vol_h24=300_000)
        make_token(s, "CALM", mc=800_000, liq=150_000, age_hours=30,
                   vol_h1=12_000, vol_h24=300_000)

    fake_http.add("/report/summary", {"score_normalised": 5, "risks": []})
    created = await signals.scan(fake_http)

    with session_scope() as s:
        rows = [(s.get(Alert, a).kind, s.get(Alert, a).token.symbol) for a in created]
    assert (AlertKind.VOLUME.value, "PUMP") in rows
    assert (AlertKind.VOLUME.value, "CALM") not in rows


# --------------------------------------------------------------------------- #
#  Karne
# --------------------------------------------------------------------------- #
def test_journal_scores_a_winning_card(rdb):
    with session_scope() as s:
        tok = make_token(s, "WINNER", mc=100_000)
        created = utcnow() - timedelta(hours=30)
        add_snapshots(s, tok, created, [
            (0, 100_000), (60, 180_000), (360, 320_000),
            (400, 330_000), (415, 330_000), (1440, 260_000),
        ])
        s.add(Alert(
            kind=AlertKind.CONFLUENCE.value, token_id=tok.id, dedupe_key="k1",
            created_at=created, entry_mc_usd=100_000, entry_price_usd=0.0001,
            entry_liquidity_usd=90_000,
        ))

    with session_scope() as s:
        journal.evaluate_alerts(s)

    with session_scope() as s:
        al = s.scalar(select(Alert).where(Alert.dedupe_key == "k1"))
        assert al.mult_1h == pytest.approx(1.8, rel=0.05)
        assert al.mult_6h == pytest.approx(3.2, rel=0.05)
        assert al.mult_24h == pytest.approx(2.6, rel=0.05)
        assert al.peak_mult == pytest.approx(3.3, rel=0.05)
        assert al.outcome == Outcome.WIN.value

        card = journal.scorecard(s, days=7)
        assert card["genel"]["isabet"] == 1
        assert card["genel"]["isabet_orani"] == pytest.approx(1.0)
        assert card["tip"][AlertKind.CONFLUENCE.value]["kapali"] == 1


def test_journal_records_a_losing_card_honestly(rdb):
    """Kotu sonuc gizlenmez; karne dusuk oran gosterir."""
    with session_scope() as s:
        tok = make_token(s, "LOSER", mc=100_000)
        created = utcnow() - timedelta(hours=30)
        add_snapshots(s, tok, created, [
            (0, 100_000), (60, 80_000), (360, 50_000), (1440, 40_000),
        ])
        s.add(Alert(
            kind=AlertKind.VOLUME.value, token_id=tok.id, dedupe_key="k2",
            created_at=created, entry_mc_usd=100_000,
        ))

    with session_scope() as s:
        journal.evaluate_alerts(s)
    with session_scope() as s:
        al = s.scalar(select(Alert).where(Alert.dedupe_key == "k2"))
        assert al.outcome == Outcome.LOSS.value
        assert al.mult_24h == pytest.approx(0.4, rel=0.05)

        card = journal.scorecard(s, days=7)
        assert card["genel"]["zarar"] == 1
        assert card["genel"]["isabet_orani"] == pytest.approx(0.0)


def test_open_cards_are_not_counted_as_wins(rdb):
    with session_scope() as s:
        tok = make_token(s, "FRESH", mc=100_000)
        s.add(Alert(
            kind=AlertKind.CONFLUENCE.value, token_id=tok.id, dedupe_key="k3",
            created_at=utcnow() - timedelta(minutes=10), entry_mc_usd=100_000,
        ))
    with session_scope() as s:
        journal.evaluate_alerts(s)
        card = journal.scorecard(s, days=7)
    assert card["genel"]["acik"] == 1
    assert card["genel"]["kapali"] == 0
    assert card["genel"]["isabet_orani"] is None
