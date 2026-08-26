"""Testler izole bir SQLite dosyasi kullanir. Bu, alpha_hunter import
edilmeden ONCE calismalidir."""
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"
os.environ["ALERTS_ENABLED"] = "false"
os.environ["TELEGRAM_BOT_TOKEN"] = ""

import pytest  # noqa: E402


@pytest.fixture()
def db():
    from alpha_hunter.db.models import Base
    from alpha_hunter.db.session import get_engine, init_db
    init_db(drop=True)
    yield
    Base.metadata.drop_all(get_engine())


# --------------------------------------------------------------------------- #
#  Radar fixtureleri (ayni veritabani dosyasi, ayri tablolar: radar_*)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def rdb():
    from radar.db import Base, get_engine, init_db

    init_db(drop=True)
    yield
    Base.metadata.drop_all(get_engine())


class FakeHttp:
    """HttpClient yerine gecer. URL parcasi -> yanit eslesmesi."""

    def __init__(self, routes: dict | None = None) -> None:
        self.routes = routes or {}
        self.calls: list = []
        self.last_error: str | None = None

    def add(self, fragment: str, payload) -> None:
        self.routes[fragment] = payload

    def _match(self, url: str):
        for frag, payload in self.routes.items():
            if frag in url:
                return payload(url) if callable(payload) else payload
        return None

    async def get(self, url, *, bucket="default", params=None, headers=None):
        self.calls.append(("GET", url, params))
        return self._match(url)

    async def post(self, url, *, bucket="default", json_body=None, params=None):
        self.calls.append(("POST", url, json_body))
        return self._match(url)

    async def request(self, method, url, **kw):
        self.calls.append((method, url, kw.get("params")))
        return self._match(url)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


@pytest.fixture()
def fake_http():
    return FakeHttp()
