"""Uctan uca dogrulama: algoritma gercekten dogru hesabi one cikariyor mu?

Uc arketip kuruyoruz ve siralamanin felsefeye uymasini bekliyoruz:
  SNIPER  — az cagri, dipten girer, ilk kesfeden, yuksek isabet
  COPYCAT — ayni tokenlar ama saatler sonra ve tepede
  SPAMMER — gunde 10 CA, birkac tutturur ama kumarbaz
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from alpha_hunter.config import settings
from alpha_hunter.db.models import Account, Call, CallOutcome, Token, TokenStatus, Tweet
from alpha_hunter.db.session import session_scope
from alpha_hunter.pipeline import repo
from alpha_hunter.scoring import metrics as M
from alpha_hunter.scoring.engine import score_account, score_all

NOW = datetime.now(timezone.utc)


def _account(s, handle):
    a = Account(platform="x", handle=handle)
    s.add(a)
    s.flush()
    return a


def _token(s, addr, supply=1_000_000_000):
    t = Token(chain="solana", address=addr, symbol=addr[:4].upper(),
              status=TokenStatus.ACTIVE, supply_estimate=supply,
              pair_created_at=NOW - timedelta(days=40))
    s.add(t)
    s.flush()
    return t


def _call(s, acc, tok, when, entry_mc, peak_mc, outcome, baseline_mc=10_000.0):
    tw = Tweet(
        account_id=acc.id, platform_tweet_id=f"{acc.id}-{tok.id}-{int(when.timestamp())}",
        posted_at=when, text=f"CA: {tok.address}", source="test",
    )
    s.add(tw)
    s.flush()
    mult = peak_mc / entry_mc
    c = Call(
        account_id=acc.id, token_id=tok.id, tweet_id=tw.id, chain="solana", called_at=when,
        entry_mc_usd=entry_mc, entry_price_usd=entry_mc / tok.supply_estimate,
        entry_liquidity_usd=25_000.0, entry_confidence=1.0, entry_source="test",
        max_mc_usd_after=peak_mc, max_multiple=mult, sustained_multiple=mult,
        outcome=outcome, is_closed=True,
    )
    c.mc_earliness = M.mc_earliness(entry_mc, settings.mc_earliness_low_usd, settings.mc_earliness_high_usd)
    c.run_capture = M.run_capture(entry_mc, peak_mc, baseline_mc, peak_mc)
    c.entry_quality = M.entry_quality(c.mc_earliness, c.run_capture)
    s.add(c)
    s.flush()
    return c


@pytest.fixture()
def archetypes(db):
    """3 arketipi ayni 6 token uzerinde kurar."""
    with session_scope() as s:
        sniper = _account(s, "sniper")
        copycat = _account(s, "copycat")
        spammer = _account(s, "spammer")

        tokens = [_token(s, f"TOK{i:039d}") for i in range(6)]

        for i, tok in enumerate(tokens):
            t0 = NOW - timedelta(days=7 * (i + 1))      # haftalara yayilmis
            peak = 2_000_000.0
            win = i < 4                                  # 6 cagrinin 4'u tutmus
            # SNIPER: 40k MC'de, ilk kesfeden
            _call(s, sniper, tok, t0, 40_000.0, peak if win else 60_000.0,
                  CallOutcome.WIN if win else CallOutcome.LOSS)
            # COPYCAT: 3 saat sonra, 900k MC'de -> ayni tepe, kucuk kat
            _call(s, copycat, tok, t0 + timedelta(hours=3), 900_000.0,
                  peak if win else 950_000.0, CallOutcome.LOSS)

        # SPAMMER: 6 gunde 60 tekil token, 12 tanesi 4x yapmis
        for i in range(60):
            tok = _token(s, f"SPAM{i:038d}")
            when = NOW - timedelta(days=6) + timedelta(hours=i * 2.4)
            won = i % 5 == 0
            _call(s, spammer, tok, when, 300_000.0,
                  1_200_000.0 if won else 200_000.0,
                  CallOutcome.WIN if won else CallOutcome.LOSS)

        for tok in s.scalars(select(Token)):
            repo.refresh_caller_ranks(s, tok.id)

    return {"sniper": "sniper", "copycat": "copycat", "spammer": "spammer"}


def _scores(handles):
    out = {}
    with session_scope() as s:
        for h in handles:
            acc = s.scalar(select(Account).where(Account.handle == h))
            out[h] = score_account(s, acc)
    return out


def test_sniper_outranks_copycat_and_spammer(archetypes):
    sc = _scores(["sniper", "copycat", "spammer"])
    assert sc["sniper"].alpha_score > sc["copycat"].alpha_score
    assert sc["sniper"].alpha_score > sc["spammer"].alpha_score


def test_copycat_has_worse_entry_quality(archetypes):
    """KURAL 1: ayni coin, ayni tepe -- tek fark giris zamani/MC'si."""
    sc = _scores(["sniper", "copycat"])
    assert sc["sniper"].entry_quality > sc["copycat"].entry_quality * 1.5


def test_copycat_loses_originality_to_first_caller(archetypes):
    """Echo cezasi: 3 saat sonra ayni CA'yi atmak ozgunlugu dusurur."""
    sc = _scores(["sniper", "copycat"])
    assert sc["sniper"].originality == pytest.approx(1.0, abs=1e-6)
    assert sc["copycat"].originality < 0.7


def test_spammer_is_penalised_for_frequency(archetypes):
    sc = _scores(["spammer"])
    assert sc["spammer"].calls_per_day > 8
    assert sc["spammer"].spray_penalty < 0.5


def test_wilson_beats_raw_win_rate_for_ranking(archetypes):
    """Spammer'in ham win-rate'i %20, sniper'in %67 -- ama asil fark
    Wilson alt sinirinda ve buyuklukte gorulur."""
    sc = _scores(["sniper", "spammer"])
    assert sc["sniper"].win_rate > sc["spammer"].win_rate
    assert sc["sniper"].wilson_lb > sc["spammer"].wilson_lb
    assert sc["sniper"].median_multiple > sc["spammer"].median_multiple


def test_caller_rank_assigned_in_time_order(archetypes):
    with session_scope() as s:
        tok = s.scalar(select(Token).where(Token.address == f"TOK{0:039d}"))
        calls = list(
            s.scalars(select(Call).where(Call.token_id == tok.id).order_by(Call.called_at))
        )
        assert [c.caller_rank for c in calls] == [1, 2]
        assert calls[0].echo_delay_sec == 0
        assert calls[1].echo_delay_sec == 3 * 3600


def test_blacklisted_account_scores_zero(archetypes):
    with session_scope() as s:
        acc = s.scalar(select(Account).where(Account.handle == "sniper"))
        acc.is_blacklisted = True
        acc.blacklist_reason = "test"
        s.flush()
        res = score_account(s, acc)
        assert res.alpha_score == 0.0
        assert res.tier == "F"


def test_score_all_persists_and_orders(archetypes):
    with session_scope() as s:
        results = score_all(s)
    assert len(results) >= 3
    assert results == sorted(results, key=lambda r: r.alpha_score, reverse=True)
    assert results[0].handle == "sniper"


def test_unrated_when_below_minimum_calls(db):
    with session_scope() as s:
        acc = _account(s, "rookie")
        tok = _token(s, "R" + "1" * 42)
        _call(s, acc, tok, NOW - timedelta(days=1), 30_000.0, 900_000.0, CallOutcome.WIN)
        res = score_account(s, acc)
        assert res.n_evaluated == 1
        assert res.tier == "UNRATED"
