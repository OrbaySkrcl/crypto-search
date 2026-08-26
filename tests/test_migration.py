"""Sema goc testleri.

Panonun 500 vermesinin sebebi: modele yeni sutun eklendiginde create_all()
VAR OLAN tabloya o sutunu eklemiyor. Eski veritabani oldugu gibi kaliyor ve
butun sorgular patliyor. sync_schema() bu bosluğu kapatir.
"""
import logging
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


# ---------------------------------------------------------------------------
# NOT NULL gevsetme
#
# sync_schema yalnizca sutun EKLER. Modelde bir sutun nullable yapilinca
# (cuzdan cagrilarinda Twitter hesabi yok -> account_id bos) var olan
# tablodaki NOT NULL kisiti oldugu yerde durur ve her ekleme
# NotNullViolation verir. sync_nullability o kisiti gevsetir.
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from alpha_hunter.db.session import (  # noqa: E402
    _drop_not_null_ddl,
    nullability_mismatches,
    sync_nullability,
)

ZORUNLU_KALAN = ("account_id", "tweet_id")


def _calls_tablosunu_not_null_kur(eng) -> None:
    """calls tablosunu account_id/tweet_id ZORUNLU olacak sekilde yeniden kurar.

    Uretimdeki PostgreSQL tablosunun hali budur: sutunlar modelde nullable
    yapilmadan once yaratildi ve o gunden beri kisit oldugu gibi duruyor.
    """
    parcalar = []
    for c in Call.__table__.columns:
        tip = c.type.compile(eng.dialect)
        satir = f'"{c.name}" {tip}'
        if c.primary_key:
            satir += " PRIMARY KEY"
        elif c.name in ZORUNLU_KALAN or not c.nullable:
            satir += " NOT NULL"
        parcalar.append(satir)
    with eng.begin() as c:
        c.execute(text("DROP TABLE calls"))
        c.execute(text(f"CREATE TABLE calls ({', '.join(parcalar)})"))


@pytest.fixture()
def not_null_veritabani(db):
    eng = get_engine()
    _calls_tablosunu_not_null_kur(eng)
    return eng


def test_a_clean_schema_has_nothing_to_relax(db):
    assert nullability_mismatches() == []
    assert sync_nullability() == []


def test_columns_the_model_relaxed_are_detected(not_null_veritabani):
    bulunan = dict.fromkeys(c for t, c in nullability_mismatches() if t == "calls")
    assert set(ZORUNLU_KALAN) <= set(bulunan)


def test_a_wallet_call_really_is_rejected_before_relaxing(not_null_veritabani):
    """REGRESYON TANIGI: kullanicinin gordugu NotNullViolation tam olarak bu.

    Zincir uzeri bir cagrinin Twitter hesabi yoktur; eski kisit onu reddeder.
    """
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        with session_scope() as s:
            t = Token(chain="solana", address="F" + "2" * 43, status=TokenStatus.ACTIVE)
            s.add(t)
            s.flush()
            s.add(Call(source=CallSource.WALLET, token_id=t.id, chain="solana",
                       called_at=NOW, buy_usd=250.0, tx_signature="sig1"))


def test_sqlite_cannot_relax_and_says_so_out_loud(not_null_veritabani, caplog):
    """Sessiz basarisizlik en kotusudur: SQLite gevsetemiyorsa bunu soylesin."""
    with caplog.at_level(logging.WARNING, logger="alpha_hunter.db.session"):
        assert sync_nullability() == []
    metin = caplog.text
    assert "calls.account_id" in metin
    assert "SQLite" in metin


def test_it_never_tightens_a_column_the_database_left_loose(db):
    """GUVENLIK: ters yon ASLA duzeltilmemeli.

    sync_schema, varsayilani olmayan zorunlu bir sutunu bilerek nullable
    ekler. Onu sonradan NOT NULL yapmak var olan satirlari gecersiz kilar.
    """
    eng = get_engine()
    with eng.begin() as c:
        c.execute(text("DROP TABLE calls"))
        parcalar = [
            f'"{col.name}" {col.type.compile(eng.dialect)}'
            + (" PRIMARY KEY" if col.primary_key else "")
            for col in Call.__table__.columns
        ]                                    # hicbiri NOT NULL degil
        c.execute(text(f"CREATE TABLE calls ({', '.join(parcalar)})"))

    zorunlu = {c.name for c in Call.__table__.columns if not c.nullable and not c.primary_key}
    assert zorunlu, "on kosul: modelde zorunlu sutun olmali"
    assert [c for t, c in nullability_mismatches() if t == "calls"] == []


def test_a_missing_table_is_skipped_not_crashed(db):
    """Sema kismen kuruluysa patlamak yerine o tabloyu atlamali."""
    with get_engine().begin() as c:
        c.execute(text("DROP TABLE calls"))
    assert nullability_mismatches() == []      # kalan tablolar temiz, hata yok


# --------------------------------------------------------- PostgreSQL yolu

class _SahteBaglanti:
    def __init__(self, kayit: list[str], patlat: bool) -> None:
        self._kayit, self._patlat = kayit, patlat

    def execute(self, stmt) -> None:
        if self._patlat:
            raise RuntimeError("izin yok")
        self._kayit.append(str(stmt))

    def __enter__(self) -> "_SahteBaglanti":
        return self

    def __exit__(self, *_) -> bool:
        return False


class _SahteMotor:
    """Postgres gibi davranan, calistirilan SQL'i kaydeden motor."""

    def __init__(self, patlat: bool = False) -> None:
        self.dialect = SimpleNamespace(name="postgresql")
        self.calisan: list[str] = []
        self._patlat = patlat

    def begin(self) -> _SahteBaglanti:
        return _SahteBaglanti(self.calisan, self._patlat)


@pytest.fixture()
def sahte_postgres(monkeypatch):
    motor = _SahteMotor()
    monkeypatch.setattr("alpha_hunter.db.session.get_engine", lambda: motor)
    monkeypatch.setattr(
        "alpha_hunter.db.session.nullability_mismatches",
        lambda: [("calls", "account_id"), ("calls", "tweet_id")],
    )
    return motor


def test_postgres_runs_one_alter_per_column(sahte_postgres):
    assert sync_nullability() == ["calls.account_id", "calls.tweet_id"]
    assert sahte_postgres.calisan == [
        'ALTER TABLE "calls" ALTER COLUMN "account_id" DROP NOT NULL',
        'ALTER TABLE "calls" ALTER COLUMN "tweet_id" DROP NOT NULL',
    ]


def test_the_ddl_quotes_identifiers(db):
    assert (
        _drop_not_null_ddl("calls", "account_id")
        == 'ALTER TABLE "calls" ALTER COLUMN "account_id" DROP NOT NULL'
    )


def test_one_failed_alter_does_not_abort_the_rest(monkeypatch, caplog):
    """Yetki yoksa acilis cokmemeli -- hatayi yazip devam etsin."""
    motor = _SahteMotor(patlat=True)
    monkeypatch.setattr("alpha_hunter.db.session.get_engine", lambda: motor)
    monkeypatch.setattr(
        "alpha_hunter.db.session.nullability_mismatches",
        lambda: [("calls", "account_id"), ("calls", "tweet_id")],
    )
    with caplog.at_level(logging.ERROR, logger="alpha_hunter.db.session"):
        assert sync_nullability() == []
    assert "calls.account_id" in caplog.text
    assert "calls.tweet_id" in caplog.text


def test_relaxing_is_idempotent(db):
    """Gevsetilmis bir semada tekrar tekrar calistirmak zararsiz olmali."""
    assert sync_nullability() == []
    assert sync_nullability() == []
    assert nullability_mismatches() == []


def test_init_db_relaxes_constraints_on_startup(db, monkeypatch):
    """Kullanicinin elle bir sey calistirmasi gerekmemeli."""
    cagrildi: list[str] = []
    monkeypatch.setattr(
        "alpha_hunter.db.session.sync_nullability",
        lambda: cagrildi.append("evet") or [],
    )
    init_db()
    assert cagrildi == ["evet"]
