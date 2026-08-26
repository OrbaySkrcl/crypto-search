"""Surekli calisan isci. Railway'de tek komutla ayaga kalkar."""
from __future__ import annotations

import asyncio
import logging
import signal

from ..config import settings
from ..db.session import init_db_when_ready, warn_if_ephemeral_storage
from .alerts import alert_fresh_calls, send_leaderboard
from .enrich import run_enrich
from .ingest import run_ingest
from .jobs import claim_next, run_job
from .score import blacklist_spammers, detect_clusters, run_scoring

log = logging.getLogger(__name__)


async def _loop(name: str, coro_factory, interval_sec: float, stop: asyncio.Event, jitter: float = 0.0) -> None:
    if jitter:
        await asyncio.sleep(jitter)
    while not stop.is_set():
        try:
            await coro_factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[%s] dongu hatasi", name)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_sec)
        except TimeoutError:
            pass


async def _ingest_tick() -> None:
    await run_ingest()
    await alert_fresh_calls(lookback_minutes=settings.ingest_interval_minutes * 3)


def _score_everything() -> None:
    from ..db.session import session_scope
    from ..onchain.linker import run_linking
    from ..scoring.engine import score_all_wallets

    run_scoring()
    if settings.onchain_enabled:
        with session_scope() as s:
            score_all_wallets(s)
            run_linking(s)


async def _score_tick() -> None:
    blacklist_spammers()
    detect_clusters()
    await asyncio.get_running_loop().run_in_executor(None, _score_everything)


async def _job_worker(stop: asyncio.Event) -> None:
    """Manuel tarama isteklerini teker teker calistirir.

    Tek isci: backfill agir bir is, paralel calistirmak ucretsiz fiyat API'sinin
    limitini yakar ve butun taramalar yavaslar.
    """
    from ..db.session import session_scope
    from ..notify.bot import announce_job_result

    await asyncio.sleep(15)
    while not stop.is_set():
        job_id = None
        try:
            with session_scope() as s:
                job = claim_next(s)
                if job is not None:
                    job_id = job.id
        except Exception:
            log.exception("is kuyrugu okunamadi")

        if job_id is None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except TimeoutError:
                pass
            continue

        try:
            await run_job(job_id)
        except Exception:
            log.exception("is calistirilamadi: #%s", job_id)
        try:
            await announce_job_result(job_id)
        except Exception:
            log.exception("is sonucu bildirilemedi: #%s", job_id)


async def _telegram_loop(stop: asyncio.Event) -> None:
    """Telegram komutlarini uzun yoklama ile dinler.

    Webhook kurmak alan adi ve sertifika ister; uzun yoklama Railway'de
    hicbir ek yapilandirma olmadan calisir.
    """
    from ..http import HttpClient
    from ..notify.bot import COMMANDS, process_updates
    from ..notify.telegram import TelegramNotifier

    notifier = TelegramNotifier()
    if not notifier.token:
        log.info("TELEGRAM_BOT_TOKEN yok, bot dinleyicisi kapali")
        return
    if not settings.telegram_chat_id:
        log.warning("TELEGRAM_CHAT_ID yok -- bot komutlari guvenlik icin reddedilecek")

    # Uzun yoklama 25sn beklediginden istemcinin zaman asimi daha uzun olmali
    async with HttpClient(timeout=45.0) as http:
        if await notifier.set_commands(COMMANDS, http):
            log.info("telegram kisayol menusu kuruldu (%d komut)", len(COMMANDS))
        log.info("telegram bot dinleniyor")
        while not stop.is_set():
            try:
                await process_updates(notifier, http)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telegram yoklama hatasi")
                await asyncio.sleep(10)


async def _ensure_db(stop: asyncio.Event) -> None:
    """Semayi arka planda hazirlar.

    Railway'de PostgreSQL uygulamadan sonra ayaga kalkiyor. Bunu ON PLANDA
    beklemek web sunucusunu geciktirir ve saglik kontrolu zaman asimina ugrar,
    o yuzden bekleme buraya alindi. Is donguleri hazir olana kadar hata verip
    bir sonraki turda tekrar dener -- kendini toparlar.
    """
    loop = asyncio.get_running_loop()
    for attempt in range(1, 6):
        if stop.is_set():
            return
        ok = await loop.run_in_executor(None, init_db_when_ready, 60.0)
        if ok:
            log.info("veritabani hazir, sema dogrulandi")
            return
        log.error("veritabanina baglanilamadi (deneme %d/5)", attempt)
    log.error("veritabani ulasilamaz durumda -- DATABASE_URL'i kontrol et")


def _make_web_server():
    """uvicorn kurulu degilse pano sessizce atlanir, isci calismaya devam eder."""
    try:
        import uvicorn

        from ..web.server import app as web_app
    except ImportError:
        log.warning("uvicorn/fastapi kurulu degil, web panosu atlaniyor")
        return None
    config = uvicorn.Config(
        web_app,
        host="0.0.0.0",
        port=settings.web_port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    return uvicorn.Server(config)


async def run_forever() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover (Windows)
            pass

    warn_if_ephemeral_storage()
    log.info(
        "Alpha Hunter calisiyor | zincir=%s kaynak=%s | ingest %ddk, enrich %ddk, score %ddk",
        ",".join(settings.chain_list), ",".join(settings.source_list),
        settings.ingest_interval_minutes, settings.enrich_interval_minutes,
        settings.score_interval_minutes,
    )

    tasks = [
        asyncio.create_task(_ensure_db(stop)),
        asyncio.create_task(
            _loop("ingest", _ingest_tick, settings.ingest_interval_minutes * 60, stop, jitter=8)
        ),
        asyncio.create_task(
            _loop("enrich", run_enrich, settings.enrich_interval_minutes * 60, stop, jitter=45)
        ),
        asyncio.create_task(
            _loop("score", _score_tick, settings.score_interval_minutes * 60, stop, jitter=120)
        ),
        asyncio.create_task(
            _loop("leaderboard", lambda: send_leaderboard(15), 24 * 3600, stop, jitter=300)
        ),
        asyncio.create_task(_job_worker(stop)),
        asyncio.create_task(_telegram_loop(stop)),
    ]
    # Web panosu ayni surecte kalkar -> Railway'de tek servis yeter
    web_server = None
    if settings.web_enabled:
        web_server = _make_web_server()
        if web_server is not None:
            tasks.append(asyncio.create_task(web_server.serve()))
            log.info("web panosu :%d portunda", settings.web_port)

    await stop.wait()
    log.info("kapatiliyor...")
    if web_server is not None:
        web_server.should_exit = True
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
