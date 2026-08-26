"""Zamanlayici — tek surec, iki asenkron gorev.

  * komut dongusu : Telegram uzun yoklama (kullaniciyi bekletmez)
  * is dongusu    : kesif → islem → fiyat → sinyal → skor → karne

Her is kendi hatasini yutar: bir kaynak dustugunde bot durmaz, o turu
atlar ve bir sonrakinde tekrar dener. Bedava API'lerle yasamanin sarti.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .bot import notify
from .bot.handlers import COMMANDS
from .bot.poller import process_updates
from .bot.telegram import Telegram
from .config import settings
from .core import discover, journal, prices, signals, trades, wallets
from .core import repo as core_repo
from .db import init_db, session_scope, set_state
from .http import HttpClient

log = logging.getLogger(__name__)


@dataclass
class Job:
    name: str
    interval_minutes: float
    fn: Callable[[HttpClient], Awaitable[dict]]
    last_run: float = 0.0
    # Ilk turda hemen calissin mi
    run_at_start: bool = True

    def due(self, now: float) -> bool:
        if self.last_run == 0.0:
            return self.run_at_start
        return (now - self.last_run) >= self.interval_minutes * 60


# --------------------------------------------------------------------------- #
#  Isler
# --------------------------------------------------------------------------- #
async def job_discover(http: HttpClient) -> dict:
    return await discover.discover_once(http)


async def job_trades(http: HttpClient) -> dict:
    return await trades.sample_trades(http)


async def job_prices(http: HttpClient) -> dict:
    return await prices.refresh_prices(http)


async def job_signals(http: HttpClient) -> dict:
    ids = await signals.scan(http)
    sent = await notify.send_alerts(ids, http)
    return {"uretilen": len(ids), "gonderilen": sent}


async def job_score(http: HttpClient) -> dict:
    return await asyncio.to_thread(wallets.run_scoring)


async def job_journal(http: HttpClient) -> dict:
    return await asyncio.to_thread(journal.run_journal)


async def job_prune(http: HttpClient) -> dict:
    def _run() -> dict:
        with session_scope() as s:
            return core_repo.prune(s)

    return await asyncio.to_thread(_run)


def build_jobs() -> list[Job]:
    return [
        Job("kesif", settings.discover_interval_minutes, job_discover),
        Job("islem", settings.trades_interval_minutes, job_trades),
        Job("fiyat", settings.price_interval_minutes, job_prices),
        Job("sinyal", max(2.0, settings.price_interval_minutes), job_signals),
        Job("skor", settings.score_interval_minutes, job_score, run_at_start=False),
        Job("karne", settings.journal_interval_minutes, job_journal, run_at_start=False),
        Job("temizlik", settings.prune_interval_minutes, job_prune, run_at_start=False),
    ]


# --------------------------------------------------------------------------- #
#  Donguler
# --------------------------------------------------------------------------- #
async def work_loop(stop: asyncio.Event) -> None:
    jobs = build_jobs()
    async with HttpClient() as http:
        while not stop.is_set():
            now = time.monotonic()
            for job in jobs:
                if stop.is_set():
                    break
                if not job.due(now):
                    continue
                job.last_run = time.monotonic()
                started = time.monotonic()
                try:
                    result = await job.fn(http)
                    took = time.monotonic() - started
                    log.info("[%s] %.1fs %s", job.name, took, result)
                    set_state(f"job:{job.name}", f"{int(time.time())}|{result}")
                except Exception as exc:                                # noqa: BLE001
                    log.exception("[%s] basarisiz: %s", job.name, exc)
                    set_state(f"job:{job.name}", f"{int(time.time())}|HATA: {exc}")
            # Kisa uyku + jitter: butun isler ayni saniyede yiginlanmasin
            try:
                await asyncio.wait_for(stop.wait(), timeout=20 + random.random() * 10)
            except TimeoutError:
                pass


async def command_loop(stop: asyncio.Event) -> None:
    tg = Telegram()
    if not tg.enabled:
        log.warning("TELEGRAM_BOT_TOKEN yok — komut dongusu kapali")
        return
    # Uzun yoklama icin daha genis zaman asimi
    async with HttpClient(timeout=45.0) as http:
        await tg.set_commands(COMMANDS, http)
        me = await tg.me(http)
        if me:
            log.info("telegram baglandi: @%s", me.get("username"))
        while not stop.is_set():
            try:
                await process_updates(http, tg)
            except Exception as exc:                                    # noqa: BLE001
                log.exception("komut dongusu hatasi: %s", exc)
                await asyncio.sleep(5)
            await asyncio.sleep(1.5)


async def run_forever() -> None:
    init_db()
    stop = asyncio.Event()
    log.info(
        "radar basliyor · zincir=%s · aktif token tavani=%s",
        ",".join(settings.chains), settings.max_active_tokens,
    )
    tasks = [
        asyncio.create_task(work_loop(stop), name="work"),
        asyncio.create_task(command_loop(stop), name="command"),
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        stop.set()
        for t in tasks:
            t.cancel()
        raise


async def run_once() -> dict:
    """Tum isleri bir kez calistirir (cron / test icin)."""
    init_db()
    out: dict = {}
    async with HttpClient() as http:
        for job in build_jobs():
            try:
                out[job.name] = await job.fn(http)
            except Exception as exc:                                    # noqa: BLE001
                out[job.name] = f"HATA: {exc}"
                log.exception("[%s] basarisiz", job.name)
    return out
