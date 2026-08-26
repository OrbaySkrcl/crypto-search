"""Radar yapilandirmasi.

Tasarim kurali: bot IKI degiskenle ayaga kalkar (TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID). Geri kalan her seyin makul bir varsayilani vardir ve
hicbiri ucretli bir servise ihtiyac duymaz.
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# GeckoTerminal ag kimlikleri <-> DexScreener zincir kimlikleri
GT_NETWORK = {
    "solana": "solana",
    "ethereum": "eth",
    "base": "base",
    "bsc": "bsc",
    "arbitrum": "arbitrum",
    "polygon": "polygon_pos",
}
GOPLUS_CHAIN_ID = {
    "ethereum": "1",
    "bsc": "56",
    "base": "8453",
    "arbitrum": "42161",
    "polygon": "137",
}
EXPLORER = {
    "solana": "https://solscan.io/token/{a}",
    "ethereum": "https://etherscan.io/token/{a}",
    "base": "https://basescan.org/token/{a}",
    "bsc": "https://bscscan.com/token/{a}",
    "arbitrum": "https://arbiscan.io/token/{a}",
    "polygon": "https://polygonscan.com/token/{a}",
}


class RadarSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ------------------------------------------------------------------ genel
    log_level: str = "INFO"
    # Izlenecek zincirler. Solana memecoin akisinin buyuk kismini tutar;
    # her ek zincir bedava API kotasindan yer yer.
    radar_chains: str = "solana"

    database_url: str = "sqlite:///radar.db"
    db_echo: bool = False

    # -------------------------------------------------------------- telegram
    telegram_bot_token: str | None = None
    # Alarmlarin gidecegi ve komut kabul edilecek TEK sohbet.
    telegram_chat_id: str | None = None

    # ------------------------------------------------------------------- http
    http_timeout: float = 25.0
    http_max_retries: int = 3
    # Bedava katman limitleri (saniyedeki istek). GeckoTerminal resmi olarak
    # 30/dk veriyor -> 0.45/sn guvenli tarafta kalir.
    rate_geckoterminal: float = 0.45
    rate_dexscreener: float = 3.0
    rate_rugcheck: float = 0.5
    rate_goplus: float = 0.5
    rate_telegram: float = 3.0

    # ----------------------------------------------------------- kesif katmani
    # Her turda kac sayfa yeni havuz cekilecek (sayfa = 20 havuz)
    discover_new_pages: int = 3
    discover_trending_pages: int = 2
    # Bu likiditenin altindaki havuz hic kaydedilmez: girilemez, olcumu de yalan.
    min_pool_liquidity_usd: float = 8_000.0
    # Bu yastan buyuk havuzlar "yeni" sayilmaz (dakika)
    max_pool_age_minutes: int = 4_320          # 3 gun
    # Ayni anda aktif izlenen token tavani. Bedava kotayi bu belirler.
    max_active_tokens: int = 300

    # ------------------------------------------------------------ islem akisi
    # Her turda kac tokenin islem defteri ornekleniyor
    trades_sample_per_cycle: int = 25
    # Bunun altindaki islemler gurultu (bot/dust)
    min_trade_usd: float = 150.0

    # --------------------------------------------------------- cuzdan skorlama
    wallet_min_evaluated: int = 4              # bu kadar sonuclanmis alim sart
    wallet_win_multiple: float = 2.0           # alimdan sonra >=2x ise isabet
    wallet_eval_hours: int = 48                # alimdan sonra bakilan pencere
    wallet_max_tokens_per_day: float = 12.0    # ustu = sprey/bot -> engelle
    wallet_min_avg_buy_usd: float = 100.0      # altı = dust/bot -> engelle
    wallet_smart_score_min: float = 60.0
    wallet_score_window_days: int = 45
    # Bir cuzdan tum tokenlerin bu oranindan fazlasinda gorunuyorsa altyapidir
    wallet_ubiquity_max_ratio: float = 0.25

    # -------------------------------------------------------------- sinyaller
    # KONFLUANS: kac farkli akilli cuzdan, kac saat icinde
    confluence_min_wallets: int = 3
    confluence_window_hours: int = 8
    confluence_cooldown_hours: int = 24        # ayni token icin tekrar araligi

    # HACIM: son 1 saat hacmi, kendi 24s saatlik ortalamasinin kac kati
    volume_spike_multiple: float = 6.0
    volume_spike_min_h1_usd: float = 40_000.0
    volume_cooldown_hours: int = 12

    # Her alarm icin ortak taban esikler
    alert_min_liquidity_usd: float = 15_000.0
    alert_max_mc_usd: float = 20_000_000.0
    alert_min_token_age_minutes: int = 20
    alert_min_safety_score: float = 0.35       # 0 = cop, 1 = temiz
    alerts_enabled: bool = True
    # Sessiz saatler (yerel degil, UTC). "" = kapali. Ornek: "23-7"
    quiet_hours_utc: str = ""

    # ---------------------------------------------------------- takip listesi
    watch_price_pct: float = 25.0              # varsayilan fiyat hareketi esigi
    watch_max_tokens: int = 50

    # ------------------------------------------------------------------ karne
    # Alarmdan sonra hangi saatlerde olculecek
    journal_hours: str = "1,6,24"
    journal_win_multiple: float = 2.0          # tepe bu kati gectiyse isabet
    journal_loss_multiple: float = 0.7         # 24s sonunda bunun altiysa zarar
    # "Kagit uzerinde tepe" elemesi: en az bu kadar dakika korunan tepe esas alinir
    sustained_minutes: int = 10

    # -------------------------------------------------------------- zamanlama
    discover_interval_minutes: int = 10
    trades_interval_minutes: int = 3
    price_interval_minutes: int = 5
    score_interval_minutes: int = 60
    journal_interval_minutes: int = 15
    prune_interval_minutes: int = 720
    snapshot_retention_days: int = 30

    # ------------------------------------------------------- opsiyonel anahtar
    # Hicbiri sart degil; varsa dogruluk artar.
    helius_api_key: str | None = None

    @field_validator("database_url")
    @classmethod
    def _normalise_db_url(cls, v: str) -> str:
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql+psycopg://", 1)
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+psycopg://", 1)
        return v

    # ------------------------------------------------------------- yardimcilar
    @property
    def chains(self) -> list[str]:
        out = [c.strip().lower() for c in self.radar_chains.split(",") if c.strip()]
        return [c for c in out if c in GT_NETWORK] or ["solana"]

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def journal_hour_list(self) -> list[int]:
        return sorted({int(x) for x in self.journal_hours.split(",") if x.strip()})

    @property
    def quiet_range(self) -> tuple[int, int] | None:
        raw = (self.quiet_hours_utc or "").strip()
        if "-" not in raw:
            return None
        a, _, b = raw.partition("-")
        try:
            return (int(a) % 24, int(b) % 24)
        except ValueError:
            return None


@lru_cache(maxsize=1)
def get_settings() -> RadarSettings:
    if not os.getenv("DATABASE_URL"):
        for alt in ("POSTGRES_URL", "DATABASE_PRIVATE_URL", "DATABASE_PUBLIC_URL"):
            if os.getenv(alt):
                os.environ["DATABASE_URL"] = os.environ[alt]
                break
    return RadarSettings()


settings = get_settings()
