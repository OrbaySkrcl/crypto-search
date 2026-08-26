"""Sema goc testleri.

Panonun 500 vermesinin sebebi: modele yeni sutun eklendiginde create_all()
VAR OLAN tabloya o sutunu eklemiyor. Eski veritabani oldugu gibi kaliyor ve
butun sorgular patliyor. sync_schema() bu bosluğu kapatir.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, text

from alpha_hunter.db.models import Account, Call, CallSource, Token, TokenStatus, Tweet
from alpha_hunter.db.session import get_engine, init_db, session_scope, sync_schema

NOW = datetime.now(timezone.utc)

# Asama 1-3'te eklenen sutunlar
YENI_SUTUNLAR = {
    "source", "wallet_id", "buy_usd", "tx_signature",
    "cohort_median_multiple", "cohort_size", "excess_multiple",
    "tradeable_usd", "tradeability",
}


def _eski_semaya_dondur(eng) -> None:
    """calls tablosunu yeni sutunlar eklenmeden onceki haliyle yeniden kurar."""
    eski = [c for c in Call.__table__.columns if c.name not in YENI_SUTUNLAR]
    ddl = ", ".join(
        f'"{c.name}" {c.type.compile(eng.dialect)}'
        + (" PRIMARY KEY" if c.primary_key else "")
        for c in eski
    )
    with eng.begin() as c:
        c.execute(text("DROP TABLE calls"))
        c.execute(text(f"CREATE TABLE calls ({ddl})"))
        c.execute(text("DROP TABLE wallet_scores"))
        c.execute(text("DROP TABLE wallets"))
        for col in ("market_edge", "median_excess", "tradeability"):
            c.execute(text(f'ALTER TABLE account_scores DROP COLUMN "{col}"'))


@pytest.fixture()
def eski_veritabani(db):
    """Icinde veri olan, eski semali bir veritabani."""
    eng = get_engine()
    _eski_semaya_dondur(eng)
    with session_scope() as s:
        a = Account(platform="x", handle="eskikayit")
        s.add(a)
        s.flush()
        t = Token(chain="solana", address="E" + "1" * 43, status=TokenStatus.ACTIVE)
        s.add(t)
        s.flush()
        w = Tweet(account_id=a.id, platform_tweet_id="1", posted_at=NOW,
                  text="CA:", source="t")
        s.add(w)
        s.flush()
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO calls (account_id, token_id, tweet_id, chain, called_at, "
                 "max_multiple, outcome, is_closed, created_at, entry_confidence) "
                 "VALUES (1,1,1,'solana',:t,7.5,'PENDING',0,:t,0.0)"),
            {"t": NOW},
        )
    return eng


def test_old_schema_is_missing_the_new_columns(eski_veritabani):
    """On kosul: gercekten eski bir sema kurduk."""
    cols = {c["name"] for c in inspect(eski_veritabani).get_columns("calls")}
    assert not (YENI_SUTUNLAR & cols)


def test_sync_adds_every_missing_column(eski_veritabani):
    eklenen = sync_schema()
    cols = {c["name"] for c in inspect(eski_veritabani).get_columns("calls")}
    assert YENI_SUTUNLAR <= cols
    assert any("calls.source" in e for e in eklenen)


def test_existing_rows_survive_the_migration(eski_veritabani):
    """Goc sirasinda tek satir veri kaybolmamali."""
    sync_schema()
    with session_scope() as s:
        c = s.query(Call).one()
        assert c.max_multiple == 7.5
        assert c.chain == "solana"


def test_enum_default_is_written_as_the_name_not_the_value(eski_veritabani):
    """REGRESYON: SQLAlchemy Enum sutunlari uyenin ADINI saklar ('TWEET').
    Varsayilani degeriyle ('tweet') yazmak, satiri okurken LookupError verir."""
    sync_schema()
    with session_scope() as s:
        c = s.query(Call).one()
        assert c.source == CallSource.TWEET      # okunabiliyor
    raw = eski_veritabani.connect().execute(text("SELECT source FROM calls")).scalar()
    assert raw == "TWEET"


def test_init_db_migrates_on_startup(eski_veritabani):
    """Kullanicinin hicbir sey yapmasina gerek kalmamali -- acilista olsun."""
    init_db()
    cols = {c["name"] for c in inspect(eski_veritabani).get_columns("calls")}
    assert YENI_SUTUNLAR <= cols
    assert inspect(eski_veritabani).has_table("wallets")


def test_every_api_query_works_after_migration(eski_veritabani):
    """Asil mesele: pano 500 vermeyi biraksin."""
    from alpha_hunter.web import queries

    init_db()
    with session_scope() as s:
        assert queries.overview(s)["calls"] == 1
        assert queries.leaderboard(s) == []
        assert len(queries.recent_calls(s, hours=720)) == 1
        assert queries.wallet_leaderboard(s) == []
        assert queries.wallet_links(s) == []


def test_sync_is_idempotent(eski_veritabani):
    sync_schema()
    assert sync_schema() == []          # ikinci kez calistirinca eklenecek sey yok


def test_sync_on_a_current_schema_changes_nothing(db):
    assert sync_schema() == []
