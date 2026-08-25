"""SQLAlchemy 2.0 semasi. SQLite (lokal) ve PostgreSQL (Railway) ile calisir.

Merkez fikir: her sey `Call` tablosunda birlesir.
Bir "call" = (hesap, token, tweet) uclusu + o andaki fiyat fotografi + sonraki
saatlerdeki fiyat hareketi. Skorlama tamamen bu tablodan turetilir.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Her zaman timezone bilgili UTC dondurur.

    SQLite tzinfo'yu saklamaz; naive datetime'lar UTC ile aware datetime'lar
    karsilastirilinca TypeError firlatir. Butun zaman mantigi T1 uzerine kurulu
    oldugu icin bu, sistemin en kritik veri tipi garantisidir.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


class TokenStatus(str, enum.Enum):
    UNKNOWN = "unknown"      # henuz zincirde dogrulanmadi
    ACTIVE = "active"
    RUGGED = "rugged"        # likidite cekildi
    DEAD = "dead"            # likidite var ama hacim yok
    INVALID = "invalid"      # DEX'te bulunamadi / token degil


class CallOutcome(str, enum.Enum):
    PENDING = "pending"      # degerlendirme penceresi acik
    WIN = "win"
    LOSS = "loss"
    RUG = "rug"
    INVALID = "invalid"      # likidite esigi altinda / fiyat cozulemedi


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Tier(str, enum.Enum):
    S = "S"
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    F = "F"
    UNRATED = "UNRATED"


# --------------------------------------------------------------------------- #
#  Hesaplar
# --------------------------------------------------------------------------- #
class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(16), default="x")
    handle: Mapped[str] = mapped_column(String(64), index=True)
    platform_user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    display_name: Mapped[str | None] = mapped_column(String(128))

    followers: Mapped[int | None] = mapped_column(Integer)
    following: Mapped[int | None] = mapped_column(Integer)
    account_created_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    # Manuel/otomatik kara liste (spam, apiklama botu, satilik promo hesabi)
    is_blacklisted: Mapped[bool] = mapped_column(Boolean, default=False)
    blacklist_reason: Mapped[str | None] = mapped_column(String(256))
    # Koordineli hareket eden hesap kumesi (ayni CA'yi dakikalar icinde atanlar)
    cluster_id: Mapped[int | None] = mapped_column(ForeignKey("account_clusters.id"), index=True)

    tweets: Mapped[list[Tweet]] = relationship(back_populates="account")
    calls: Mapped[list[Call]] = relationship(back_populates="account")
    scores: Mapped[list[AccountScore]] = relationship(back_populates="account")

    __table_args__ = (UniqueConstraint("platform", "handle", name="uq_account_platform_handle"),)


class AccountCluster(Base):
    """Ayni tokenlari surekli birlikte paylasan hesaplar = muhtemel promo agi."""
    __tablename__ = "account_clusters"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str | None] = mapped_column(String(128))
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    cohesion: Mapped[float] = mapped_column(Float, default=0.0)  # ortalama Jaccard
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    notes: Mapped[dict | None] = mapped_column(JSON)


# --------------------------------------------------------------------------- #
#  Tokenlar
# --------------------------------------------------------------------------- #
class Token(Base):
    __tablename__ = "tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    chain: Mapped[str] = mapped_column(String(24), index=True)
    address: Mapped[str] = mapped_column(String(64), index=True)

    symbol: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(128))
    decimals: Mapped[int | None] = mapped_column(Integer)

    # Ana likidite havuzu (fiyat gecmisi bu havuzdan cekilir)
    pair_address: Mapped[str | None] = mapped_column(String(64), index=True)
    dex_id: Mapped[str | None] = mapped_column(String(48))
    pair_created_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # price -> market cap cevrimi icin (memecoinlerde arz sabit kabul edilir)
    supply_estimate: Mapped[float | None] = mapped_column(Float)

    status: Mapped[TokenStatus] = mapped_column(
        Enum(TokenStatus, native_enum=False, length=16), default=TokenStatus.UNKNOWN, index=True
    )

    launch_mc_usd: Mapped[float | None] = mapped_column(Float)
    ath_mc_usd: Mapped[float | None] = mapped_column(Float)
    ath_mc_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_mc_usd: Mapped[float | None] = mapped_column(Float)
    last_liquidity_usd: Mapped[float | None] = mapped_column(Float)
    peak_liquidity_usd: Mapped[float | None] = mapped_column(Float)

    # Anti-scam katmani
    security: Mapped[dict | None] = mapped_column(JSON)
    security_score: Mapped[float | None] = mapped_column(Float)   # 0 (cop) .. 1 (temiz)
    security_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    history_backfilled: Mapped[bool] = mapped_column(Boolean, default=False)

    calls: Mapped[list[Call]] = relationship(back_populates="token")

    __table_args__ = (
        UniqueConstraint("chain", "address", name="uq_token_chain_address"),
        Index("ix_token_status_refresh", "status", "last_refreshed_at"),
    )


class PriceSnapshot(Base):
    """Zaman serisi. OHLCV'den turetilen veya canli cekilen noktalar."""
    __tablename__ = "price_snapshots"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)

    price_usd: Mapped[float] = mapped_column(Float)
    high_usd: Mapped[float | None] = mapped_column(Float)
    low_usd: Mapped[float | None] = mapped_column(Float)
    mc_usd: Mapped[float | None] = mapped_column(Float)
    liquidity_usd: Mapped[float | None] = mapped_column(Float)
    volume_usd: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(24))

    __table_args__ = (
        UniqueConstraint("token_id", "ts", "source", name="uq_snapshot_token_ts_source"),
        Index("ix_snapshot_token_ts", "token_id", "ts"),
    )


# --------------------------------------------------------------------------- #
#  Tweetler
# --------------------------------------------------------------------------- #
class Tweet(Base):
    __tablename__ = "tweets"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    platform_tweet_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)

    # T1 -- butun algoritmanin dayandigi zaman damgasi. Her zaman UTC.
    posted_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    text: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(256))
    lang: Mapped[str | None] = mapped_column(String(8))

    is_retweet: Mapped[bool] = mapped_column(Boolean, default=False)
    is_reply: Mapped[bool] = mapped_column(Boolean, default=False)
    is_quote: Mapped[bool] = mapped_column(Boolean, default=False)

    like_count: Mapped[int | None] = mapped_column(Integer)
    retweet_count: Mapped[int | None] = mapped_column(Integer)
    reply_count: Mapped[int | None] = mapped_column(Integer)
    view_count: Mapped[int | None] = mapped_column(Integer)

    source: Mapped[str] = mapped_column(String(24))         # nitter / apify / xapi / twscrape
    raw: Mapped[dict | None] = mapped_column(JSON)
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    account: Mapped[Account] = relationship(back_populates="tweets")
    calls: Mapped[list[Call]] = relationship(back_populates="tweet")

    __table_args__ = (Index("ix_tweet_account_posted", "account_id", "posted_at"),)


# --------------------------------------------------------------------------- #
#  CALL -- sistemin kalbi
# --------------------------------------------------------------------------- #
class Call(Base):
    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), index=True)
    tweet_id: Mapped[int] = mapped_column(ForeignKey("tweets.id"), index=True)
    chain: Mapped[str] = mapped_column(String(24), index=True)

    called_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)

    # --- KURAL 1: tweet anindaki fotograf (T1) ---------------------------- #
    entry_price_usd: Mapped[float | None] = mapped_column(Float)
    entry_mc_usd: Mapped[float | None] = mapped_column(Float)
    entry_liquidity_usd: Mapped[float | None] = mapped_column(Float)
    entry_source: Mapped[str | None] = mapped_column(String(24))
    # 1.0 = tweet dakikasinda gercek OHLCV mumu bulundu, 0.3 = tahmin
    entry_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    # Tweet'ten ONCEKI tepe -> copycat tespiti
    pre_ath_mc_usd: Mapped[float | None] = mapped_column(Float)
    pre_ath_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    token_age_at_call_sec: Mapped[int | None] = mapped_column(Integer)

    # --- Sonuc ------------------------------------------------------------ #
    max_mc_usd_after: Mapped[float | None] = mapped_column(Float)
    max_mc_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    max_multiple: Mapped[float | None] = mapped_column(Float)
    # ATH kagit uzerinde kalir; bu, en az N dakika korunan tepeye gore
    sustained_multiple: Mapped[float | None] = mapped_column(Float)
    mc_by_window: Mapped[dict | None] = mapped_column(JSON)      # {"1": mc, "24": mc, ...}
    multiple_by_window: Mapped[dict | None] = mapped_column(JSON)
    max_drawdown_after: Mapped[float | None] = mapped_column(Float)

    # --- Turetilmis metrikler --------------------------------------------- #
    # Coin'in toplam log-yukselisinin ne kadarini yakaladi? 0..1
    run_capture: Mapped[float | None] = mapped_column(Float)
    # Mutlak MC seviyesine gore erkencilik 0..1
    mc_earliness: Mapped[float | None] = mapped_column(Float)
    entry_quality: Mapped[float | None] = mapped_column(Float)   # ikisinin bilesigi

    # --- KURAL: echo / copycat -------------------------------------------- #
    caller_rank: Mapped[int | None] = mapped_column(Integer)     # 1 = bildigimiz ilk cagiran
    echo_delay_sec: Mapped[int | None] = mapped_column(Integer)  # ilk cagriya gore gecikme
    originality: Mapped[float | None] = mapped_column(Float)     # exp(-delay/tau)

    outcome: Mapped[CallOutcome] = mapped_column(
        Enum(CallOutcome, native_enum=False, length=16), default=CallOutcome.PENDING, index=True
    )
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    invalid_reason: Mapped[str | None] = mapped_column(String(128))

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    entry_resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    account: Mapped[Account] = relationship(back_populates="calls")
    token: Mapped[Token] = relationship(back_populates="calls")
    tweet: Mapped[Tweet] = relationship(back_populates="calls")

    __table_args__ = (
        # Ayni tweet ayni tokeni iki kez cagirmis sayilmasin
        UniqueConstraint("tweet_id", "token_id", name="uq_call_tweet_token"),
        Index("ix_call_account_called", "account_id", "called_at"),
        Index("ix_call_token_called", "token_id", "called_at"),
        Index("ix_call_pending", "is_closed", "last_evaluated_at"),
    )


# --------------------------------------------------------------------------- #
#  Skorlar
# --------------------------------------------------------------------------- #
class AccountScore(Base):
    __tablename__ = "account_scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    window_days: Mapped[int] = mapped_column(Integer)

    n_calls: Mapped[int] = mapped_column(Integer, default=0)
    n_evaluated: Mapped[int] = mapped_column(Integer, default=0)
    n_wins: Mapped[int] = mapped_column(Integer, default=0)
    n_moons: Mapped[int] = mapped_column(Integer, default=0)
    n_rugs: Mapped[int] = mapped_column(Integer, default=0)

    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    # Kucuk orneklem cezalandirilmis guven alt siniri -- asil siralama metrigi
    wilson_lb: Mapped[float] = mapped_column(Float, default=0.0)
    median_multiple: Mapped[float] = mapped_column(Float, default=0.0)
    p90_multiple: Mapped[float] = mapped_column(Float, default=0.0)
    avg_entry_mc_usd: Mapped[float | None] = mapped_column(Float)

    magnitude: Mapped[float] = mapped_column(Float, default=0.0)
    entry_quality: Mapped[float] = mapped_column(Float, default=0.0)
    survivorship: Mapped[float] = mapped_column(Float, default=0.0)
    originality: Mapped[float] = mapped_column(Float, default=0.0)
    consistency: Mapped[float] = mapped_column(Float, default=0.0)

    calls_per_day: Mapped[float] = mapped_column(Float, default=0.0)
    spray_penalty: Mapped[float] = mapped_column(Float, default=1.0)
    data_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    alpha_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    tier: Mapped[Tier] = mapped_column(
        Enum(Tier, native_enum=False, length=8), default=Tier.UNRATED, index=True
    )
    breakdown: Mapped[dict | None] = mapped_column(JSON)

    account: Mapped[Account] = relationship(back_populates="scores")

    __table_args__ = (
        UniqueConstraint("account_id", "computed_at", name="uq_score_account_time"),
        Index("ix_score_latest", "account_id", "computed_at"),
    )


# --------------------------------------------------------------------------- #
#  Isletme kayitlari
# --------------------------------------------------------------------------- #
class IngestRun(Base):
    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(24))
    query: Mapped[str | None] = mapped_column(String(512))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    tweets_seen: Mapped[int] = mapped_column(Integer, default=0)
    tweets_new: Mapped[int] = mapped_column(Integer, default=0)
    calls_new: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(160), index=True)
    payload: Mapped[dict | None] = mapped_column(JSON)
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (UniqueConstraint("kind", "dedupe_key", name="uq_alert_kind_key"),)


class Job(Base):
    """Arka plan is kuyrugu.

    Manuel hesap taramasi (backfill) dakikalar surebilir; ne web istegini ne de
    Telegram mesajini bekletebiliriz. Her iki arayuz de buraya is birakir,
    zamanlayicidaki isci tek tek calistirir.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)      # 'backfill'
    target: Mapped[str] = mapped_column(String(128))               # handle
    params: Mapped[dict | None] = mapped_column(JSON)              # {"days": 60}
    source: Mapped[str] = mapped_column(String(16), default="web") # web | telegram | cli

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=16), default=JobStatus.QUEUED, index=True
    )
    progress: Mapped[str | None] = mapped_column(String(256))
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Telegram'dan geldiyse sonucu buraya bildir
    notify_chat_id: Mapped[str | None] = mapped_column(String(48))

    __table_args__ = (Index("ix_job_status_created", "status", "created_at"),)


class AppState(Base):
    """Kucuk kalici anahtar-deger deposu (ornegin Telegram update offseti)."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
