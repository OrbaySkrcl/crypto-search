"""Surekli calisan isci. Railway'de tek komutla ayaga kalkar."""
from __future__ import annotations

import asyncio
import logging
import signal

from ..config import settings
from .alerts import alert_fresh_calls, send_leaderboard
from .enrich import run_enrich
from .ingest import run_ingest
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


async def _score_tick() -> None:
    blacklist_spammers()
    detect_clusters()
    await asyncio.get_running_loop().run_in_executor(None, run_scoring)


def _make_web_server():
    """uvicorn kurulu degilse pano sessizce atlanir, isci calismaya devam eder."""
    try:
        import uvicorn

        from ..web.app import app as web_app
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

    log.info(
        "Alpha Hunter calisiyor | zincir=%s kaynak=%s | ingest %ddk, enrich %ddk, score %ddk",
        ",".join(settings.chain_list), ",".join(settings.source_list),
        settings.ingest_interval_minutes, settings.enrich_interval_minutes,
        settings.score_interval_minutes,
    )

    tasks = [
        asyncio.create_task(_loop("ingest", _ingest_tick, settings.ingest_interval_minutes * 60, stop)),
        asyncio.create_task(
            _loop("enrich", run_enrich, settings.enrich_interval_minutes * 60, stop, jitter=45)
        ),
        asyncio.create_task(
            _loop("score", _score_tick, settings.score_interval_minutes * 60, stop, jitter=120)
        ),
        asyncio.create_task(
            _loop("leaderboard", lambda: send_leaderboard(15), 24 * 3600, stop, jitter=300)
        ),
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
