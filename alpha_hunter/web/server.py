"""Web panosu: tarayicidan canli liderlik tablosu.

Terminal bilmeye gerek yok. Railway'de `run` komutu bu sunucuyu da baslatir.
Sifre korumasi opsiyoneldir (WEB_PASSWORD tanimliysa devreye girer).
"""
from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from ..config import settings
from ..db.session import db_status, healthcheck, init_db_when_ready, session_scope
from . import queries

log = logging.getLogger(__name__)
_security = HTTPBasic(auto_error=False)
_INDEX = Path(__file__).parent / "static" / "index.html"


def _check_auth(credentials: HTTPBasicCredentials | None = Depends(_security)) -> None:
    """WEB_PASSWORD bos ise koruma yok; doluysa HTTP Basic zorunlu."""
    if not settings.web_password:
        return
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="giris gerekli",
            headers={"WWW-Authenticate": "Basic"},
        )
    ok_user = secrets.compare_digest(credentials.username, settings.web_user)
    ok_pass = secrets.compare_digest(credentials.password, settings.web_password)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="kullanici adi veya sifre hatali",
            headers={"WWW-Authenticate": "Basic"},
        )


def _log_db_result(fut) -> None:
    if fut.cancelled():
        return
    exc = fut.exception()
    if exc is not None:
        log.error("sema hazirligi basarisiz: %s", exc)
    elif fut.result():
        log.info("veritabani hazir, sema dogrulandi")
    else:
        log.error("veritabanina baglanilamadi -- DATABASE_URL'i kontrol et")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Sema hazirligi ON PLANDA beklenmez: uvicorn startup bitmeden baglanti
    # kabul etmez, bu da Railway saglik kontrolunu zaman asimina ugratir.
    # Sema ya zamanlayici tarafindan ya da asagidaki arka plan gorevi ile kurulur.
    import asyncio

    # run_in_executor zaten isi zamanlar ve bir Future dondurur -- create_task
    # coroutine bekledigi icin buraya sarmalanmaz.
    fut = asyncio.get_running_loop().run_in_executor(None, init_db_when_ready, 120.0)
    fut.add_done_callback(_log_db_result)
    log.info("web panosu acildi")
    try:
        yield
    finally:
        fut.cancel()


def create_app() -> FastAPI:
    app = FastAPI(
        lifespan=_lifespan,
        title="Alpha Hunter",
        description="Memecoin alfa hesap avcisi — canli pano",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
    )

    # ---------------------------------------------------------------- sayfa
    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(_check_auth)])
    def index() -> HTMLResponse:
        if not _INDEX.exists():
            return HTMLResponse("<h1>arayuz dosyasi bulunamadi</h1>", status_code=500)
        return HTMLResponse(_INDEX.read_text(encoding="utf-8"))

    # ----------------------------------------------------------------- api
    @app.get("/api/overview", dependencies=[Depends(_check_auth)])
    def api_overview() -> JSONResponse:
        with session_scope() as s:
            return JSONResponse(queries.overview(s))

    @app.get("/api/leaderboard", dependencies=[Depends(_check_auth)])
    def api_leaderboard(
        limit: int = Query(50, ge=1, le=500),
        tier: str | None = Query(None, pattern="^[SABCDF]$"),
    ) -> JSONResponse:
        with session_scope() as s:
            return JSONResponse(queries.leaderboard(s, limit=limit, min_tier=tier))

    @app.get("/api/account/{handle}", dependencies=[Depends(_check_auth)])
    def api_account(handle: str, limit: int = Query(60, ge=1, le=500)) -> JSONResponse:
        with session_scope() as s:
            data = queries.account_detail(s, handle, limit=limit)
        if data is None:
            raise HTTPException(status_code=404, detail=f"@{handle} bulunamadi")
        return JSONResponse(data)

    @app.get("/api/calls", dependencies=[Depends(_check_auth)])
    def api_calls(
        hours: int = Query(24, ge=1, le=720),
        limit: int = Query(100, ge=1, le=500),
        min_alpha: float = Query(0.0, ge=0.0, le=100.0),
    ) -> JSONResponse:
        with session_scope() as s:
            return JSONResponse(
                queries.recent_calls(s, hours=hours, limit=limit, min_alpha=min_alpha)
            )

    @app.get("/health")
    def health() -> JSONResponse:
        """Surec ayaktaysa 200 doner; veritabani durumu govdede raporlanir.

        Kasitli olarak veritabanina baglanamasa bile 200 doner: Railway bu ucu
        'konteyner yanit veriyor mu' diye sorar, veritabani birkac saniye sonra
        hazir olacaksa dagitimi basarisiz saymamali.

        Veritabani durumu ONBELLEKTEN okunur; canli yoklama yapilsa ulasilamayan
        bir sunucuda istek zaman asimina kadar asili kalir ve kontrol yine duser.
        """
        st = db_status()
        return JSONResponse({"ok": True, "db": st["ok"], "db_checked_ago": st["checked_seconds_ago"]})

    @app.get("/health/db")
    def health_db() -> JSONResponse:
        """Kati kontrol: veritabani erisilebilir degilse 503."""
        ok = healthcheck()
        return JSONResponse({"ok": ok}, status_code=200 if ok else 503)

    return app


app = create_app()


def serve(host: str = "0.0.0.0", port: int | None = None) -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=host,
        port=port or settings.web_port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
