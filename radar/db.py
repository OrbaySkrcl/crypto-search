"""Radar veritabani: modeller + oturum fabrikasi.

Tablolar `radar_` onekli, boylece ayni veritabanini alpha_hunter ile
paylasabilir. SQLite ile bedava calisir; Postgres verirsen o da calisir.
"""
from __future__ import annotations

import enum
import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from .config import settings

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """SQLite naive datetime dondurur; her zaman UTC-aware'e cevir."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class TokenStatus(str, enum.Enum):
    LIVE = "live"
    DEAD = "dead"          # likidite cekilmis / islem yok
    RUG = "rug"


class AlertKind(str, enum.Enum):
    CONFLUENCE = "confluence"
    VOLUME = "volume"
    WATCH_PRICE = "watch_price"
    WATCH_SMART = "watch_smart"


class Outcome(str, enum.Enum):
    OPEN = "open"
    WIN = "win"
    FLAT = "flat"
    LOSS = "loss"


# --------------------------------------------------------------------------- #
#  Token
# --------------------------------------------------------------------------- #
class Token(Base):
    __tablename__ = "radar_tokens"
    __table_args__ = (
        UniqueConstraint("chain", "address", name="uq_radar_token"),
        Index("ix_radar_token_active", "active", "last_checked_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(24), index=True)
    address: Mapped[str] = mapped_column(String(96), index=True)
    symbol: Mapped[str | None] = mapped_column(String(48))
    name: Mapped[str | None] = mapped_column(String(160))
    pair_address: Mapped[str | None] = mapped_column(String(96))
    dex_id: Mapped[str | None] = mapped_column(String(48))

    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    pool_created_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    first_price_usd: Mapped[float | None] = mapped_column(Float)
    first_mc_usd: Mapped[float | None] = mapped_column(Float)
    first_liquidity_usd: Mapped[float | None] = mapped_column(Float)

    last_price_usd: Mapped[float | None] = mapped_column(Float)
    last_mc_usd: Mapped[float | None] = mapped_column(Float)
    last_liquidity_usd: Mapped[float | None] = mapped_column(Float)
    last_volume_h1: Mapped[float | None] = mapped_column(Float)
    last_volume_h24: Mapped[float | None] = mapped_column(Float)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # "Kagit uzerinde tepe" degil: en az sustained_minutes korunmus tepe.
    peak_mc_usd: Mapped[float | None] = mapped_column(Float)
    peak_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    status: Mapped[TokenStatus] = mapped_column(String(12), default=TokenStatus.LIVE)
    # Aktif = fiyati duzenli ornekleniyor. Kota bu bayrakla korunur.
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    safety_score: Mapped[float | None] = mapped_column(Float)
    safety_flags: Mapped[str | None] = mapped_column(Text)          # json list
    safety_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    trades_synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    def flags(self) -> list[str]:
        try:
            return json.loads(self.safety_flags or "[]")
        except ValueError:
            return []


class PriceSnapshot(Base):
    """Kendi fiyat gecmisimiz. Ucuncu taraf tarihsel API'ye bagimlilik yok."""

    __tablename__ = "radar_price_snapshots"
    __table_args__ = (Index("ix_radar_snap_token_at", "token_id", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("radar_tokens.id", ondelete="CASCADE"))
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    price_usd: Mapped[float | None] = mapped_column(Float)
    mc_usd: Mapped[float | None] = mapped_column(Float)
    liquidity_usd: Mapped[float | None] = mapped_column(Float)
    volume_h1: Mapped[float | None] = mapped_column(Float)


# --------------------------------------------------------------------------- #
#  Cuzdan
# --------------------------------------------------------------------------- #
class Wallet(Base):
    __tablename__ = "radar_wallets"
    __table_args__ = (
        UniqueConstraint("chain", "address", name="uq_radar_wallet"),
        Index("ix_radar_wallet_smart", "smart", "score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(24), index=True)
    address: Mapped[str] = mapped_column(String(96), index=True)
    label: Mapped[str | None] = mapped_column(String(48))          # kod adi

    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    n_tokens: Mapped[int] = mapped_column(Integer, default=0)
    n_buys: Mapped[int] = mapped_column(Integer, default=0)
    n_sells: Mapped[int] = mapped_column(Integer, default=0)
    avg_buy_usd: Mapped[float | None] = mapped_column(Float)

    n_evaluated: Mapped[int] = mapped_column(Integer, default=0)
    n_wins: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float | None] = mapped_column(Float)
    wilson: Mapped[float | None] = mapped_column(Float)
    median_multiple: Mapped[float | None] = mapped_column(Float)
    median_entry_mc: Mapped[float | None] = mapped_column(Float)

    score: Mapped[float] = mapped_column(Float, default=0.0)
    smart: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    block_reason: Mapped[str | None] = mapped_column(String(80))
    scored_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    def short(self) -> str:
        return f"{self.address[:5]}…{self.address[-4:]}" if len(self.address) > 12 else self.address


class Trade(Base):
    """Ornekleme ile toplanan takas kayitlari."""

    __tablename__ = "radar_trades"
    __table_args__ = (
        UniqueConstraint("token_id", "wallet_id", "tx_hash", name="uq_radar_trade"),
        Index("ix_radar_trade_at", "at"),
        Index("ix_radar_trade_wallet_at", "wallet_id", "at"),
        Index("ix_radar_trade_token_at", "token_id", "at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("radar_tokens.id", ondelete="CASCADE"))
    wallet_id: Mapped[int] = mapped_column(ForeignKey("radar_wallets.id", ondelete="CASCADE"))
    chain: Mapped[str] = mapped_column(String(24))
    side: Mapped[str] = mapped_column(String(8))                   # buy | sell
    at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    usd: Mapped[float | None] = mapped_column(Float)
    price_usd: Mapped[float | None] = mapped_column(Float)
    mc_usd: Mapped[float | None] = mapped_column(Float)
    tx_hash: Mapped[str] = mapped_column(String(128))

    # Sonuc (skorlama doldurur): alimdan sonraki korunmus tepe / giris
    result_multiple: Mapped[float | None] = mapped_column(Float)
    evaluated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    token: Mapped[Token] = relationship(lazy="joined")
    wallet: Mapped[Wallet] = relationship(lazy="joined")


# --------------------------------------------------------------------------- #
#  Kullanici tarafi
# --------------------------------------------------------------------------- #
class Watch(Base):
    __tablename__ = "radar_watch"
    __table_args__ = (UniqueConstraint("chat_id", "token_id", name="uq_radar_watch"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(48), index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("radar_tokens.id", ondelete="CASCADE"))
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    price_pct: Mapped[float | None] = mapped_column(Float)         # None = genel ayar
    muted: Mapped[bool] = mapped_column(Boolean, default=False)
    ref_price_usd: Mapped[float | None] = mapped_column(Float)
    ref_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    token: Mapped[Token] = relationship(lazy="joined")


class Alert(Base):
    """Gonderilen her kart burada. Karne bu tablodan cikar."""

    __tablename__ = "radar_alerts"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_radar_alert_dedupe"),
        Index("ix_radar_alert_kind_at", "kind", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("radar_tokens.id", ondelete="CASCADE"))
    chat_id: Mapped[str | None] = mapped_column(String(48))
    dedupe_key: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)

    entry_price_usd: Mapped[float | None] = mapped_column(Float)
    entry_mc_usd: Mapped[float | None] = mapped_column(Float)
    entry_liquidity_usd: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[str | None] = mapped_column(Text)              # json

    mult_1h: Mapped[float | None] = mapped_column(Float)
    mult_6h: Mapped[float | None] = mapped_column(Float)
    mult_24h: Mapped[float | None] = mapped_column(Float)
    peak_mult: Mapped[float | None] = mapped_column(Float)         # korunmus tepe / giris
    outcome: Mapped[str] = mapped_column(String(8), default=Outcome.OPEN.value, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    token: Mapped[Token] = relationship(lazy="joined")

    def data(self) -> dict:
        try:
            return json.loads(self.payload or "{}")
        except ValueError:
            return {}


class State(Base):
    """Kucuk anahtar/deger deposu (telegram offset, son kosu zamanlari)."""

    __tablename__ = "radar_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------- #
#  Engine / oturum
# --------------------------------------------------------------------------- #
_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None
_init_lock = threading.Lock()
_ready = False


def get_engine() -> Engine:
    global _engine
    if _engine is not None:
        return _engine
    kwargs: dict = {"echo": settings.db_echo, "future": True}
    if settings.is_postgres:
        kwargs.update(
            pool_size=5, max_overflow=10, pool_pre_ping=True, pool_recycle=1800,
            connect_args={"connect_timeout": 5},
        )
    else:
        kwargs.update(connect_args={"check_same_thread": False, "timeout": 30})
    _engine = create_engine(settings.database_url, **kwargs)

    if not settings.is_postgres:
        @event.listens_for(_engine, "connect")
        def _pragmas(dbapi_conn, _):           # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _factory
    if _factory is None:
        _factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _factory


def init_db(drop: bool = False) -> None:
    """Semayi kurar ve eksik sutunlari ekler (hafif gocu)."""
    global _ready
    with _init_lock:
        eng = get_engine()
        if drop:
            Base.metadata.drop_all(eng)
        Base.metadata.create_all(eng, checkfirst=True)
        _migrate(eng)
        _ready = True


def _migrate(eng: Engine) -> None:
    """Yeni surumde eklenen sutunlari var olan tablolara ekler.

    Alembic kurmadan, tek yonlu ve zararsiz: yalnizca ADD COLUMN.
    """
    insp = inspect(eng)
    existing = set(insp.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(eng.dialect)}"
            try:
                with eng.begin() as conn:
                    conn.execute(text(ddl))
                log.info("sema gocu: %s.%s eklendi", table.name, col.name)
            except Exception as exc:                                    # noqa: BLE001
                log.warning("sema gocu atlandi (%s.%s): %s", table.name, col.name, exc)


@contextmanager
def session_scope() -> Iterator[Session]:
    if not _ready:
        init_db()
    s = get_session_factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_state(key: str, default: str | None = None) -> str | None:
    with session_scope() as s:
        row = s.get(State, key)
        return row.value if row and row.value is not None else default


def set_state(key: str, value: str) -> None:
    with session_scope() as s:
        row = s.get(State, key)
        if row is None:
            s.add(State(key=key, value=value))
        else:
            row.value = value
            row.updated_at = utcnow()


def db_healthy() -> bool:
    try:
        with get_engine().connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:                                                   # noqa: BLE001
        return False


__all__ = [
    "Alert", "AlertKind", "Base", "Outcome", "PriceSnapshot", "State", "Token",
    "TokenStatus", "Trade", "Wallet", "Watch", "db_healthy", "get_engine",
    "get_state", "init_db", "session_scope", "set_state", "timedelta", "utcnow",
]
