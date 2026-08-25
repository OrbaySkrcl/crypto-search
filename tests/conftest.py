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
