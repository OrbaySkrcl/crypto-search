"""Merkezi konfigurasyon. Her sey environment degiskenlerinden okunur (Railway uyumlu)."""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Chain = Literal["solana", "base", "ethereum", "bsc", "arbitrum", "polygon"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ---------------------------------------------------------------- genel
    env: str = "dev"
    log_level: str = "INFO"
    # Birincil zincir. Coklu zincir icin virgullu liste: "solana,base"
    chains: str = "solana"

    # ------------------------------------------------------------ veritabani
    # Railway PostgreSQL icin: postgresql+psycopg://... (Railway DATABASE_URL'i
    # postgresql:// verir, asagida otomatik cevriliyor)
    database_url: str = "sqlite:///alpha_hunter.db"
    db_echo: bool = False

    # -------------------------------------------------------------- ingestion
    # Aktif kaynaklar, oncelik sirasiyla: nitter, apify, xapi, twscrape
    tweet_sources: str = "nitter"
    ingest_lookback_minutes: int = 90
    ingest_max_tweets_per_run: int = 800

    nitter_instances: str = (
        "https://nitter.net,https://nitter.poast.org,https://xcancel.com,"
        "https://nitter.privacyredirect.com,https://lightbrd.com"
    )
    apify_token: str | None = None
    # NOT: apidojo/tweet-scraper odemeli bir aktor ve kiralama/deneme suresi
    # dolunca hata vermeden BOS donuyor. Lite surumu ucretsiz katmanda calisiyor.
    apify_actor: str = "apidojo/twitter-scraper-lite"
    # Apify sonuc basina ucretlendirir. Zamanlanmis dongu her 10 dakikada bir
    # calistigi icin fatura sessizce buyuyebilir; gunluk tavan koyuyoruz.
    # 0 = sinirsiz.
    apify_daily_tweet_budget: int = 4000
    # Aktor isini bitirene kadar beklenecek toplam sure. Twitter taramasi
    # dakikalar surebilir; kisa tutmak "ucreti oder ama sonucu alma"ya yol acar.
    apify_max_wait_seconds: int = 420
    apify_request_timeout: float = 60.0
    x_bearer_token: str | None = None
    twscrape_db: str = "twscrape_accounts.db"

    # Aranacak sorgular (satir sonu veya | ile ayrilir)
    search_queries: str = (
        'pump.fun/coin | dexscreener.com/solana | "CA:" solana | "contract:" solana | $SOL memecoin CA'
    )
    # Ek olarak takip edilen hesaplar (virgullu handle listesi, @ olmadan)
    watchlist_handles: str = ""

    # ------------------------------------------------------------ price oracle
    birdeye_api_key: str | None = None
    helius_api_key: str | None = None
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    dexscreener_base: str = "https://api.dexscreener.com"
    geckoterminal_base: str = "https://api.geckoterminal.com/api/v2"
    birdeye_base: str = "https://public-api.birdeye.so"
    rugcheck_base: str = "https://api.rugcheck.xyz/v1"

    http_timeout: float = 25.0
    http_max_retries: int = 4
    # Kaynak basina saniyedeki maksimum istek (token bucket)
    rate_dexscreener: float = 4.0
    rate_geckoterminal: float = 0.5
    rate_birdeye: float = 8.0
    rate_rugcheck: float = 1.0
    rate_rpc: float = 8.0

    # ------------------------------------------------------- degerlendirme
    # Bir cagrinin "kapandigi" ana kadar bakilan pencere
    eval_windows_hours: str = "1,6,24,72,168"
    eval_close_after_hours: int = 168          # 7 gun sonra call kapanir
    # ATH yerine "gercekci cikis": en az bu kadar dakika korunan tepe
    sustained_high_minutes: int = 15
    win_multiple: float = 3.0                  # >=3x ise "win"
    moon_multiple: float = 10.0                # 10x = moon
    min_entry_liquidity_usd: float = 2_000.0   # bunun altinda call gecersiz
    max_entry_mc_usd: float = 50_000_000.0     # bunun ustunde cagri "gec" sayilir
    rug_liquidity_floor_usd: float = 800.0
    rug_liquidity_drop_pct: float = 0.90

    # -------------------------------------------------------------- skorlama
    score_window_days: int = 120
    score_halflife_days: float = 45.0
    min_calls_for_rating: int = 4
    spray_soft_calls_per_day: float = 3.0
    spray_hard_calls_per_day: float = 25.0
    spray_min_penalty: float = 0.25
    echo_tau_seconds: float = 21_600.0         # 6 saat
    mc_earliness_low_usd: float = 15_000.0
    mc_earliness_high_usd: float = 10_000_000.0

    w_reliability: float = 0.28
    w_magnitude: float = 0.18
    w_market_edge: float = 0.16      # kohortu ne kadar gecti
    w_entry_quality: float = 0.18
    w_survivorship: float = 0.10
    w_originality: float = 0.10

    # --- piyasa cipasi ---
    cohort_window_hours: int = 6      # ayni gun/saat cagrilan diger tokenlar
    cohort_max_window_hours: int = 72 # yeterli ornek yoksa pencere buraya kadar genisler
    cohort_min_size: int = 6          # bundan azsa cipa uygulanmaz

    # --- alinabilirlik ---
    max_slippage: float = 0.05        # %5 kaymayla ne kadar dolar girilebilir
    tradeable_floor_usd: float = 100.0    # bunun altinda pratikte girilemez
    tradeable_target_usd: float = 10_000.0  # bu seviyede tam puan

    # -------------------------------------------------------------- alarmlar
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    alert_min_alpha_score: float = 65.0        # A tier ve ustu
    alert_max_entry_mc_usd: float = 3_000_000.0
    alerts_enabled: bool = True

    # ------------------------------------------------------------ web panosu
    web_enabled: bool = True
    web_port: int = 8000
    web_user: str = "admin"
    # Bos birakilirsa pano sifresiz acilir. Railway'de mutlaka doldur.
    web_password: str | None = None

    # ------------------------------------------------------------- scheduler
    ingest_interval_minutes: int = 10
    enrich_interval_minutes: int = 5
    score_interval_minutes: int = 60
    leaderboard_cron_hour: int = 9

    # ------------------------------------------------------------ validators
    @field_validator("database_url")
    @classmethod
    def _normalise_db_url(cls, v: str) -> str:
        # Railway / Heroku stili URL'leri SQLAlchemy 2.x surucusune cevir
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql+psycopg://", 1)
        elif v.startswith("postgresql://"):
            v = v.replace("postgresql://", "postgresql+psycopg://", 1)
        return v

    # ------------------------------------------------------------- yardimci
    @property
    def chain_list(self) -> list[str]:
        return [c.strip().lower() for c in self.chains.split(",") if c.strip()]

    @property
    def source_list(self) -> list[str]:
        return [s.strip().lower() for s in self.tweet_sources.split(",") if s.strip()]

    @property
    def nitter_list(self) -> list[str]:
        return [u.strip().rstrip("/") for u in self.nitter_instances.split(",") if u.strip()]

    @property
    def query_list(self) -> list[str]:
        raw = self.search_queries.replace("\n", "|")
        return [q.strip() for q in raw.split("|") if q.strip()]

    @property
    def watchlist(self) -> list[str]:
        return [h.strip().lstrip("@").lower() for h in self.watchlist_handles.split(",") if h.strip()]

    @property
    def eval_window_list(self) -> list[int]:
        return [int(x) for x in self.eval_windows_hours.split(",") if x.strip()]

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # Railway bazen DATABASE_URL yerine POSTGRES_URL enjekte eder
    if not os.getenv("DATABASE_URL"):
        for alt in ("POSTGRES_URL", "DATABASE_PRIVATE_URL", "DATABASE_PUBLIC_URL"):
            if os.getenv(alt):
                os.environ["DATABASE_URL"] = os.environ[alt]
                break
    # Railway/Heroku web portunu PORT ile verir
    if not os.getenv("WEB_PORT") and os.getenv("PORT"):
        os.environ["WEB_PORT"] = os.environ["PORT"]
    return Settings()


settings = get_settings()
