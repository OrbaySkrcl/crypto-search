"""Apify istemcisi testleri (agsiz).

Buradaki en kritik test: senkron uca (run-sync-get-dataset-items) BIR DAHA
gidilmemesi. O uc aktor bitene kadar baglantiyi acik tutuyordu; 25 saniyelik
istemci zaman asimi devreye girince Apify tarafinda is calismaya devam edip
UCRETLENDIRILIYOR, bize hicbir sonuc donmuyordu. Ustelik max_retries=2 bunu
ucla carpiyordu.
"""
from datetime import datetime, timedelta, timezone

import pytest

from alpha_hunter.config import settings
from alpha_hunter.http import HttpClient
from alpha_hunter.ingest.apify import ApifySource

SINCE = datetime.now(timezone.utc) - timedelta(days=7)
RUN_ID = "run123"
DS_ID = "ds456"


def _tweet_item(i=0):
    return {
        "id": f"180000000{i}",
        "url": f"https://x.com/testuser/status/180000000{i}",
        "text": f"CA: EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm gem {i}",
        "createdAt": (datetime.now(timezone.utc) - timedelta(hours=i + 1)).strftime(
            "%a %b %d %H:%M:%S +0000 %Y"
        ),
        "author": {"userName": "testuser", "id": "42", "followers": 1234},
        "likeCount": 10,
    }


class FakeApifyHttp(HttpClient):
    """Apify'in uc adimli akisini taklit eder ve cagrilari kaydeder."""

    def __init__(self, statuses=None, items=None, start_fails=False):
        super().__init__()
        self.calls: list[dict] = []
        self._statuses = list(statuses or ["SUCCEEDED"])
        self._items = items if items is not None else [_tweet_item(0), _tweet_item(1)]
        self._start_fails = start_fails

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url, *, bucket="default", json_body=None, headers=None,
                   max_retries=None, timeout=None):
        self.calls.append({"m": "POST", "url": url, "retries": max_retries,
                           "timeout": timeout, "body": json_body})
        if "/runs" in url:
            if self._start_fails:
                return {"error": {"type": "actor-not-found", "message": "aktor bulunamadi"}}
            return {"data": {"id": RUN_ID, "defaultDatasetId": DS_ID, "status": "RUNNING"}}
        return None

    async def get(self, url, *, bucket="default", params=None, headers=None,
                  expect_json=True, max_retries=None, timeout=None):
        self.calls.append({"m": "GET", "url": url, "retries": max_retries,
                           "timeout": timeout, "params": params})
        if "/users/me" in url:
            return {"data": {"username": "testhesap"}}
        if f"/actor-runs/{RUN_ID}" in url:
            st = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
            return {"data": {"status": st}}
        if f"/datasets/{DS_ID}/items" in url:
            return self._items
        return None

    def urls(self, method=None):
        return [c["url"] for c in self.calls if method is None or c["m"] == method]


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """Yoklama beklemesini sifirla, testler aninda kossun."""
    import alpha_hunter.ingest.apify as mod

    async def no_sleep(_):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(settings, "apify_token", "apify_api_TESTTOKEN")


# --------------------------------------------------------------- regresyon
async def test_never_uses_the_blocking_sync_endpoint(db):
    """REGRESYON: senkron uc, ucreti odetip sonucu vermeyen zaman asimina
    yol aciyordu. Bir daha kullanilmamali."""
    http = FakeApifyHttp()
    src = ApifySource(http)
    [t async for t in src.user_timeline("testuser", SINCE, 10)]

    assert not any("run-sync" in u for u in http.urls()), \
        "senkron uc geri gelmis -- ucret odenip sonuc alinamayan hataya doner"


async def test_follows_start_poll_fetch_sequence(db):
    http = FakeApifyHttp(statuses=["RUNNING", "RUNNING", "SUCCEEDED"])
    src = ApifySource(http)
    got = [t async for t in src.user_timeline("testuser", SINCE, 10)]

    urls = http.urls()
    assert any(u.endswith("/runs") for u in urls)                 # 1) baslat
    assert any(f"/actor-runs/{RUN_ID}" in u for u in urls)        # 2) yokla
    assert any(f"/datasets/{DS_ID}/items" in u for u in urls)     # 3) al
    assert len(got) == 2
    assert got[0].handle == "testuser"


async def test_billable_start_is_never_retried(db):
    """Her yeniden deneme YENI bir ucretli aktor calismasi baslatir."""
    http = FakeApifyHttp()
    src = ApifySource(http)
    [t async for t in src.user_timeline("testuser", SINCE, 10)]

    start = next(c for c in http.calls if c["m"] == "POST" and c["url"].endswith("/runs"))
    assert start["retries"] == 0


async def test_requests_use_a_long_timeout(db):
    """25 saniyelik varsayilan, dakikalarca suren bir aktor icin yetersiz."""
    http = FakeApifyHttp()
    src = ApifySource(http)
    [t async for t in src.user_timeline("testuser", SINCE, 10)]

    start = next(c for c in http.calls if c["url"].endswith("/runs"))
    assert start["timeout"] is not None
    assert start["timeout"] >= 30


# ------------------------------------------------------------- hata yollari
async def test_start_failure_is_explained(db):
    http = FakeApifyHttp(start_fails=True)
    src = ApifySource(http)
    got = [t async for t in src.user_timeline("testuser", SINCE, 10)]

    assert got == []
    assert "aktor baslatilamadi" in (src.last_detail or "")
    assert "actor-not-found" in (src.last_detail or "")


async def test_empty_dataset_is_explained(db):
    http = FakeApifyHttp(items=[])
    src = ApifySource(http)
    got = [t async for t in src.user_timeline("testuser", SINCE, 10)]

    assert got == []
    assert "0 kayit" in (src.last_detail or "")


async def test_failed_run_status_is_reported(db):
    http = FakeApifyHttp(statuses=["FAILED"], items=[])
    src = ApifySource(http)
    [t async for t in src.user_timeline("testuser", SINCE, 10)]
    assert "FAILED" in (src.last_detail or "")


async def test_timeline_falls_back_to_from_search(db):
    """Ilk girdi bicimi bos donerse 'from:handle' aramasi denenmeli."""
    class TwoStep(FakeApifyHttp):
        def __init__(self):
            super().__init__()
            self._n = 0

        async def post(self, url, **kw):
            if "/runs" in url:
                self._n += 1
                self._items = [] if self._n == 1 else [_tweet_item(0)]
            return await super().post(url, **kw)

    http = TwoStep()
    src = ApifySource(http)
    got = [t async for t in src.user_timeline("testuser", SINCE, 10)]

    starts = [c for c in http.calls if c["m"] == "POST" and c["url"].endswith("/runs")]
    assert len(starts) == 2
    assert "twitterHandles" in starts[0]["body"]
    assert starts[1]["body"]["searchTerms"] == ["from:testuser"]
    assert len(got) == 1


# ------------------------------------------------------------------ butce
async def test_budget_blocks_source_when_exhausted(db, monkeypatch):
    monkeypatch.setattr(settings, "apify_daily_tweet_budget", 2)
    import alpha_hunter.ingest.apify as mod
    monkeypatch.setattr(mod, "budget_used", lambda: 5)

    src = ApifySource(FakeApifyHttp())
    assert await src.available() is False
    assert "butcesi doldu" in (src.last_detail or "")


async def test_available_reports_missing_token(db, monkeypatch):
    monkeypatch.setattr(settings, "apify_token", None)
    src = ApifySource(FakeApifyHttp(), token=None)
    assert await src.available() is False
    assert "APIFY_TOKEN" in (src.last_detail or "")


async def test_budget_counter_increments(db, monkeypatch):
    monkeypatch.setattr(settings, "apify_daily_tweet_budget", 1000)
    import alpha_hunter.ingest.apify as mod

    before = mod.budget_used()
    src = ApifySource(FakeApifyHttp())
    [t async for t in src.user_timeline("testuser", SINCE, 10)]
    assert mod.budget_used() == before + 2


# --------------------------------------------------------------- teshis
async def test_diagnose_reports_a_working_setup(db):
    http = FakeApifyHttp()
    out = await ApifySource(http).diagnose("testuser")

    assert out["token_var"] is True
    assert out["token_gecerli"] is True
    assert out["hesap"] == "testhesap"
    assert out["kayit_sayisi"] == 2
    assert out["cozumlenebildi"] is True
    assert out["sonuc"] == "calisiyor"
    assert "ornek_alanlar" in out


async def test_diagnose_reports_a_broken_actor(db):
    out = await ApifySource(FakeApifyHttp(start_fails=True)).diagnose("testuser")
    assert out["token_gecerli"] is True
    assert out["kayit_sayisi"] == 0
    assert "aktor baslatilamadi" in (out["aciklama"] or "")


async def test_diagnose_without_token(db, monkeypatch):
    monkeypatch.setattr(settings, "apify_token", None)
    out = await ApifySource(FakeApifyHttp(), token=None).diagnose()
    assert out["token_var"] is False
    assert "APIFY_TOKEN" in out["sonuc"]


# ------------------------------------------------ cozumleme teshisi
def test_why_normalise_failed_names_the_missing_field():
    from alpha_hunter.ingest.apify import _why_normalise_failed

    assert "kullanici adi" in _why_normalise_failed(
        {"id": "1", "createdAt": "2024-01-01T00:00:00Z"}
    )
    assert "tarih alani" in _why_normalise_failed({"id": "1", "author": {"userName": "x"}})
    assert "tarih cozulemedi" in _why_normalise_failed(
        {"id": "1", "author": {"userName": "x"}, "createdAt": "dun"}
    )
    assert "tweet kimligi" in _why_normalise_failed(
        {"author": {"userName": "x"}, "createdAt": "2024-01-01T00:00:00Z"}
    )
    # saglam kayitta hata yok
    assert _why_normalise_failed(_tweet_item(0)) is None


def test_normalise_handles_nested_graphql_author():
    """Bazi aktorler X'in GraphQL yanitini oldugu gibi geciriyor."""
    from alpha_hunter.ingest.apify import _normalise

    t = _normalise({
        "rest_id": "1826",
        "full_text": "CA: test",
        "created_at": "Tue Aug 20 12:00:00 +0000 2024",
        "core": {"user_results": {"result": {"legacy": {"screen_name": "godofgem"}}}},
    })
    assert t is not None
    assert t.handle == "godofgem"


def test_handle_recovered_from_url_when_author_missing():
    from alpha_hunter.ingest.apify import _normalise

    t = _normalise({
        "id": "1826",
        "url": "https://x.com/godofgem/status/1826",
        "text": "x",
        "createdAt": "2024-08-20T12:00:00Z",
    })
    assert t is not None and t.handle == "godofgem"


async def test_diagnose_explains_unparseable_records(db):
    """Kayit geliyor ama okunamiyorsa: neden okunamadigi + alan adlari."""
    bozuk = [{"tweetText": "merhaba", "postedOn": "dun", "writer": "godofgem"}]
    out = await ApifySource(FakeApifyHttp(items=bozuk)).diagnose("godofgem")

    assert out["kayit_sayisi"] == 1
    assert out["cozumlenebildi"] is False
    assert out["cozumleme_hatasi"]
    assert "tweetText" in out["ornek_alanlar"]
    assert "ham_ornek" in out
