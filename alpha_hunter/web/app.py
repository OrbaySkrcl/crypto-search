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
from ..db.session import healthcheck, init_db, session_scope
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


@asynccontextmanager
async def _lifespan(app: FastAPI):
    init_db()
    log.info("web panosu hazir")
    yield


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
