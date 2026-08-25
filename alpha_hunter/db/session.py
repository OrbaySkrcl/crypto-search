"""Engine / session fabrikasi ve sema olusturma."""
from __future__ import annotations

import logging
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


def get_engine() -> Engine:
    global _engine
    if _engine is not None:
        return _engine

    kwargs: dict = {"echo": settings.db_echo, "future": True}
    if settings.is_postgres:
        kwargs.update(pool_size=5, max_overflow=10, pool_pre_ping=True, pool_recycle=1800)
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
    if drop:
        log.warning("Tum tablolar siliniyor")
        Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    log.info("Sema hazir: %s", settings.database_url.split("@")[-1])


def healthcheck() -> bool:
    try:
        with get_engine().connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # pragma: no cover
        log.error("DB healthcheck basarisiz: %s", exc)
        return False
