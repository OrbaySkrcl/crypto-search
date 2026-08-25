"""Web panosu ve JSON API testleri."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from alpha_hunter.config import settings
from alpha_hunter.db.models import Account, Call, CallOutcome, Token, TokenStatus, Tweet
from alpha_hunter.db.session import session_scope
from alpha_hunter.pipeline.score import run_scoring

NOW = datetime.now(timezone.utc)


@pytest.fixture()
def client(db):
    from alpha_hunter.web.server import create_app
    return TestClient(create_app())


@pytest.fixture()
def seeded(db):
    with session_scope() as s:
        acc = Account(platform="x", handle="tester", followers=5000)
        s.add(acc)
        s.flush()
        for i in range(6):
            tok = Token(chain="solana", address=f"T{i:043d}", symbol=f"TK{i}",
                        status=TokenStatus.ACTIVE, supply_estimate=1e9, security_score=0.8)
            s.add(tok)
            s.flush()
            when = NOW - timedelta(days=i * 5 + 1)
            tw = Tweet(account_id=acc.id, platform_tweet_id=f"tw{i}", posted_at=when,
                       text="CA:", source="test", url=f"https://x.com/tester/status/{i}")
            s.add(tw)
            s.flush()
            won = i < 4
            s.add(Call(
                account_id=acc.id, token_id=tok.id, tweet_id=tw.id, chain="solana",
                called_at=when, entry_mc_usd=50_000.0, entry_price_usd=5e-5,
                entry_liquidity_usd=30_000.0, entry_confidence=1.0,
                max_mc_usd_after=1_000_000.0 if won else 60_000.0,
                max_multiple=20.0 if won else 1.2,
                sustained_multiple=18.0 if won else 1.1,
                entry_quality=0.7, run_capture=0.8, originality=1.0, caller_rank=1,
                outcome=CallOutcome.WIN if won else CallOutcome.LOSS, is_closed=True,
            ))
    run_scoring()
    from alpha_hunter.web.server import create_app
    return TestClient(create_app())


def test_health(client):
    from alpha_hunter.db.session import healthcheck
    healthcheck()                     # onbellegi tazele
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["db"] is True


def test_health_stays_200_when_db_is_down(client, monkeypatch):
    """Railway saglik kontrolu 'konteyner yanit veriyor mu' diye sorar.
    Veritabani birkac saniye gec kalksa dagitim basarisiz sayilmamali."""
    monkeypatch.setattr("alpha_hunter.web.server.healthcheck", lambda: False)
    monkeypatch.setattr(
        "alpha_hunter.web.server.db_status",
        lambda: {"ok": False, "checked_seconds_ago": 3.0},
    )
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["db"] is False
    # kati uc ise 503 dondurur
    assert client.get("/health/db").status_code == 503


def test_health_never_probes_the_database(client, monkeypatch):
    """Ulasilamayan bir sunucuda canli yoklama istegi kilitler; /health
    onbellekten okumali ve healthcheck()'i HIC cagirmamali."""
    called = {"n": 0}

    def boom():
        called["n"] += 1
        raise AssertionError("/health canli yoklama yapmamali")

    monkeypatch.setattr("alpha_hunter.web.server.healthcheck", boom)
    assert client.get("/health").status_code == 200
    assert called["n"] == 0


def test_wait_for_db_retries_then_succeeds(monkeypatch):
    from alpha_hunter.db import session as sess

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        return calls["n"] >= 3

    monkeypatch.setattr(sess, "healthcheck", flaky)
    assert sess.wait_for_db(timeout=30.0, interval=0.0) is True
    assert calls["n"] == 3


def test_wait_for_db_gives_up_and_returns_false(monkeypatch):
    from alpha_hunter.db import session as sess

    monkeypatch.setattr(sess, "healthcheck", lambda: False)
    assert sess.wait_for_db(timeout=0.0, interval=0.0) is False


def test_index_serves_dashboard(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "ALPHA" in r.text
    assert "Liderlik tablosu" in r.text


def test_overview_on_empty_db(client):
    d = client.get("/api/overview").json()
    assert d["accounts"] == 0
    assert d["calls"] == 0
    assert d["tiers"] == {}
    assert d["chains"] == settings.chain_list


def test_leaderboard_returns_scored_account(seeded):
    rows = seeded.get("/api/leaderboard").json()
    assert len(rows) == 1
    r = rows[0]
    assert r["handle"] == "tester"
    assert r["n_evaluated"] == 6
    assert r["n_wins"] == 4
    assert r["alpha_score"] > 0
    assert r["median_multiple"] > 1


def test_leaderboard_tier_filter(seeded):
    assert seeded.get("/api/leaderboard?tier=S").json() == []
    assert seeded.get("/api/leaderboard?tier=F").json() != []
    assert seeded.get("/api/leaderboard?tier=Z").status_code == 422


def test_account_detail(seeded):
    d = seeded.get("/api/account/tester").json()
    assert d["handle"] == "tester"
    assert len(d["calls"]) == 6
    assert d["score"]["n_wins"] == 4
    assert d["calls"][0]["tweet_url"].startswith("https://x.com/")
    assert seeded.get("/api/account/@tester").status_code == 200


def test_account_not_found(client):
    assert client.get("/api/account/yokboyle").status_code == 404


def test_recent_calls_window_and_filter(seeded):
    assert len(seeded.get("/api/calls?hours=48").json()) == 1
    assert len(seeded.get("/api/calls?hours=720").json()) == 6
    assert seeded.get("/api/calls?hours=720&min_alpha=99").json() == []


def test_calls_validation(client):
    assert client.get("/api/calls?hours=0").status_code == 422
    assert client.get("/api/calls?min_alpha=500").status_code == 422


def test_password_protection(db, monkeypatch):
    monkeypatch.setattr(settings, "web_password", "gizli")
    monkeypatch.setattr(settings, "web_user", "admin")
    from alpha_hunter.web.server import create_app
    c = TestClient(create_app())
    assert c.get("/api/overview").status_code == 401
    assert c.get("/api/overview", auth=("admin", "yanlis")).status_code == 401
    assert c.get("/api/overview", auth=("admin", "gizli")).status_code == 200
    # health korumasizdir -- Railway saglik kontrolu icin
    assert c.get("/health").status_code == 200


# --------------------------------------------------------- gecici disk uyarisi
def test_warns_when_railway_uses_sqlite(monkeypatch, caplog):
    """Railway'de PostgreSQL baglanmazsa veri her dagitimda silinir.
    Bu sessizce olmamali."""
    import logging

    from alpha_hunter.db import session as sess

    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "abc123")
    monkeypatch.setattr(sess.settings, "database_url", "sqlite:///alpha.db")
    with caplog.at_level(logging.ERROR):
        assert sess.warn_if_ephemeral_storage() is True
    joined = " ".join(r.message for r in caplog.records)
    assert "PostgreSQL bagli degil" in joined
    assert "DATABASE_URL" in joined


def test_no_warning_on_postgres(monkeypatch):
    from alpha_hunter.db import session as sess

    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "abc123")
    monkeypatch.setattr(sess.settings, "database_url", "postgresql+psycopg://u:p@h/db")
    assert sess.warn_if_ephemeral_storage() is False


def test_no_warning_outside_railway(monkeypatch):
    from alpha_hunter.db import session as sess

    for k in list(__import__("os").environ):
        if k.startswith("RAILWAY_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(sess.settings, "database_url", "sqlite:///local.db")
    assert sess.warn_if_ephemeral_storage() is False
