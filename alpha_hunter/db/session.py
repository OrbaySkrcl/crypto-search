"""Engine / session fabrikasi ve sema olusturma."""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import settings
from .models import Base

log = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None

# Son bilinen veritabani durumu. /health bunu okur -- canli yoklama YAPMAZ,
# cunku ulasilamayan bir sunucu istegi zaman asimina kadar asili birakir ve
# Railway saglik kontrolu duser.
_db_ok: bool = False
_db_checked_at: float = 0.0

# Zamanlayici ve web sunucusu semayi ayni anda kurmaya calisabilir; SQLite'ta
# bu "table ... already exists" hatasi verir. Tek seferde bir tane calissin.
_init_lock = threading.Lock()


def get_engine() -> Engine:
    global _engine
    if _engine is not None:
        return _engine

    kwargs: dict = {"echo": settings.db_echo, "future": True}
    if settings.is_postgres:
        kwargs.update(
            pool_size=5, max_overflow=10, pool_pre_ping=True, pool_recycle=1800,
            # Ulasilamayan bir sunucuda varsayilan TCP zaman asimi ~2 dakikadir;
            # bu, saglik kontrolunu ve acilisi kilitler.
            connect_args={"connect_timeout": 5},
        )
    else:
        kwargs.update(connect_args={"check_same_thread": False, "timeout": 30})

    _engine = create_engine(settings.database_url, **kwargs)

    if not settings.is_postgres:
        @event.listens_for(_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    s = get_session_factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db(drop: bool = False) -> None:
    eng = get_engine()
    with _init_lock:
        if drop:
            log.warning("Tum tablolar siliniyor")
            Base.metadata.drop_all(eng)
        # checkfirst=True varsayilan olsa da es zamanli iki cagri yarisabilir;
        # kilit + yeniden deneme ikisini de kapatir.
        try:
            Base.metadata.create_all(eng, checkfirst=True)
        except Exception as exc:
            if "already exists" not in str(exc).lower():
                raise
            log.debug("sema zaten kurulmus (es zamanli cagri): %s", exc)
    log.info("Sema hazir: %s", settings.database_url.split("@")[-1])


def wait_for_db(timeout: float = 90.0, interval: float = 3.0) -> bool:
    """Veritabani ayaga kalkana kadar bekler.

    Railway'de PostgreSQL eklentisi uygulamadan ~30sn sonra hazir oluyor.
    Bu bekleme olmadan uygulama aciliste cokup yeniden baslatma dongusune giriyor.
    """
    import time

    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        if healthcheck():
            if attempt:
                log.info("veritabani %d denemeden sonra hazir", attempt + 1)
            return True
        attempt += 1
        if time.monotonic() >= deadline:
            log.error("veritabani %.0f saniyede hazir olmadi", timeout)
            return False
        log.warning("veritabani henuz hazir degil, %.0fsn sonra tekrar denenecek", interval)
        time.sleep(interval)


def init_db_when_ready(timeout: float = 90.0) -> bool:
    """Baglanti kurulana kadar bekleyip semayi olusturur. Hata firlatmaz."""
    if not wait_for_db(timeout):
        return False
    try:
        init_db()
        return True
    except Exception:
        log.exception("sema olusturulamadi")
        return False


def healthcheck() -> bool:
    """Canli yoklama. Ulasilamayan sunucuda connect_timeout kadar surer."""
    global _db_ok, _db_checked_at
    import time

    try:
        with get_engine().connect() as c:
            c.execute(text("SELECT 1"))
        ok = True
    except Exception as exc:
        log.debug("DB healthcheck basarisiz: %s", exc)
        ok = False
    _db_ok, _db_checked_at = ok, time.monotonic()
    return ok


def db_status() -> dict:
    """Onbellekli durum -- aga hic dokunmaz, aninda doner."""
    import time

    return {
        "ok": _db_ok,
        "checked_seconds_ago": (
            round(time.monotonic() - _db_checked_at, 1) if _db_checked_at else None
        ),
    }
