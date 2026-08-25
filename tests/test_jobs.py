"""Is kuyrugu ve manuel tarama testleri."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from alpha_hunter.config import settings
from alpha_hunter.db.models import Job, JobStatus
from alpha_hunter.db.session import session_scope
from alpha_hunter.pipeline import jobs as jobq


# ----------------------------------------------------------- handle ayiklama
@pytest.mark.parametrize("raw,expected", [
    ("elonmusk", "elonmusk"),
    ("@elonmusk", "elonmusk"),
    ("  @ElonMusk  ", "elonmusk"),
    ("https://x.com/elonmusk", "elonmusk"),
    ("https://twitter.com/elonmusk/status/123", "elonmusk"),
    ("x.com/elonmusk?s=20", "elonmusk"),
    ("user_name_1", "user_name_1"),
])
def test_normalise_handle_accepts_common_forms(raw, expected):
    assert jobq.normalise_handle(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "@", "cok-uzun-olmayan-ama-gecersiz", "bosluk var",
    "a" * 16,          # Twitter siniri 15
    "hesap!", "e-mail@x.com",
])
def test_normalise_handle_rejects_invalid(raw):
    assert jobq.normalise_handle(raw) is None


@pytest.mark.parametrize("raw,expected", [
    (60, 60), (1, 1), (365, 365),
    (0, 1), (-5, 1), (9999, 365),
    (None, 60), ("30", 30), ("abc", 60),
])
def test_clamp_days(raw, expected):
    assert jobq.clamp_days(raw) == expected


# -------------------------------------------------------------- kuyruk
def test_enqueue_creates_queued_job(db):
    job, msg = jobq.enqueue_backfill("@TestUser", 30, source="web")
    assert job is not None
    assert job.target == "testuser"
    assert job.params == {"days": 30}
    assert job.status == JobStatus.QUEUED
    assert "kuyruga alindi" in msg


def test_enqueue_rejects_bad_handle(db):
    job, msg = jobq.enqueue_backfill("bosluk var", 30)
    assert job is None
    assert "gecersiz" in msg


def test_enqueue_is_deduplicated(db):
    first, _ = jobq.enqueue_backfill("dedupe", 60)
    second, msg = jobq.enqueue_backfill("dedupe", 60)
    assert second.id == first.id
    assert "zaten kuyrukta" in msg
    with session_scope() as s:
        assert len(list(s.scalars(select(Job)))) == 1


def test_claim_next_marks_running_and_is_fifo(db):
    a, _ = jobq.enqueue_backfill("first", 10)
    b, _ = jobq.enqueue_backfill("second", 10)
    with session_scope() as s:
        claimed = jobq.claim_next(s)
        assert claimed.target == "first"
        assert claimed.status == JobStatus.RUNNING
        assert claimed.started_at is not None
        nxt = jobq.claim_next(s)
        assert nxt.target == "second"
        assert jobq.claim_next(s) is None


def test_finish_records_result_and_error(db):
    job, _ = jobq.enqueue_backfill("done", 10)
    jobq.finish(job.id, result={"found": True, "alpha_score": 71.2})
    with session_scope() as s:
        row = s.get(Job, job.id)
        assert row.status == JobStatus.DONE
        assert row.result["alpha_score"] == 71.2
        assert row.finished_at is not None

    other, _ = jobq.enqueue_backfill("failer", 10)
    jobq.finish(other.id, error="RuntimeError: patladi")
    with session_scope() as s:
        row = s.get(Job, other.id)
        assert row.status == JobStatus.FAILED
        assert "patladi" in row.error


def test_progress_is_truncated(db):
    job, _ = jobq.enqueue_backfill("prog", 10)
    jobq.set_progress(job.id, "x" * 500)
    with session_scope() as s:
        assert len(s.get(Job, job.id).progress) <= 250


# ------------------------------------------------------------------ web api
@pytest.fixture()
def client(db):
    from alpha_hunter.web.server import create_app
    return TestClient(create_app())


def test_backfill_endpoint_queues_job(client):
    r = client.post("/api/backfill", json={"handle": "@SomeTrader", "days": 45})
    assert r.status_code == 202
    body = r.json()
    assert body["job"]["target"] == "sometrader"
    assert body["job"]["days"] == 45
    assert body["job"]["status"] == "queued"
    assert body["job"]["source"] == "web"


def test_backfill_endpoint_validates_handle(client):
    assert client.post("/api/backfill", json={"handle": "bad handle"}).status_code == 422
    assert client.post("/api/backfill", json={"handle": ""}).status_code == 422
    assert client.post("/api/backfill", json={}).status_code == 422


def test_backfill_endpoint_clamps_days(client):
    r = client.post("/api/backfill", json={"handle": "clamped", "days": 99999})
    assert r.json()["job"]["days"] == 365


def test_jobs_listing_and_detail(client):
    created = client.post("/api/backfill", json={"handle": "listed", "days": 20}).json()
    job_id = created["job"]["id"]

    rows = client.get("/api/jobs").json()
    assert any(j["id"] == job_id for j in rows)

    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["target"] == "listed"
    assert client.get("/api/jobs/999999").status_code == 404


def test_backfill_requires_auth_when_password_set(db, monkeypatch):
    monkeypatch.setattr(settings, "web_password", "gizli")
    monkeypatch.setattr(settings, "web_user", "admin")
    from alpha_hunter.web.server import create_app
    c = TestClient(create_app())
    assert c.post("/api/backfill", json={"handle": "x"}).status_code == 401
    assert c.get("/api/jobs").status_code == 401
    ok = c.post("/api/backfill", json={"handle": "authed"}, auth=("admin", "gizli"))
    assert ok.status_code == 202


# ------------------------------------------------- bos sonucun gercek sebebi
def test_diagnose_wrong_chain_is_the_first_suspect(monkeypatch):
    """En sinsi durum: adres bulundu, dogrulandi, ama takip edilmeyen zincirde.
    Kullaniciya 'CA yok' demek yanlis yone gonderir."""
    monkeypatch.setattr(settings, "chains", "solana")
    msg = jobq.diagnose_empty_result({
        "tweets_seen": 40, "tweets_with_ca": 12, "ca_candidates": 12,
        "skipped_wrong_chain": 12, "chains_seen": {"ethereum": 12},
    })
    assert "ethereum" in msg
    assert "CHAINS" in msg
    assert "solana" in msg


def test_diagnose_no_tweets_points_at_the_source():
    """Kaynak kaydi varsa onu aktarir; tahmin yurutmez."""
    msg = jobq.diagnose_empty_result({
        "tweets_seen": 0, "tweets_with_ca": 0, "ca_candidates": 0,
        "skipped_wrong_chain": 0, "chains_seen": {},
        "source_attempts": ["apify: kapali", "nitter: 0 tweet — hicbiri yanit vermedi"],
    })
    assert "Hic tweet cekilemedi" in msg
    assert "apify: kapali" in msg


def test_diagnose_tweets_but_no_contract():
    msg = jobq.diagnose_empty_result({
        "tweets_seen": 55, "tweets_with_ca": 0, "ca_candidates": 0,
        "skipped_wrong_chain": 0, "chains_seen": {},
    })
    assert "55 tweet" in msg
    assert "kontrat adresi yok" in msg


def test_diagnose_candidates_that_failed_dex_verification():
    msg = jobq.diagnose_empty_result({
        "tweets_seen": 30, "tweets_with_ca": 4, "ca_candidates": 6,
        "skipped_wrong_chain": 0, "chains_seen": {},
    })
    assert "dogrulanamadi" in msg


def test_slim_stats_keeps_only_diagnostic_fields():
    slim = jobq._slim_stats({
        "tweets_seen": 10, "tweets_with_ca": 2, "ca_candidates": 3,
        "calls_new": 1, "skipped_wrong_chain": 0, "chains_seen": {"solana": 1},
        "tweets_new": 10, "rejected": 5, "tokens_new": 1,
    })
    assert set(slim) == {
        "tweets_seen", "tweets_with_ca", "ca_candidates",
        "calls_new", "skipped_wrong_chain", "chains_seen",
    }


def test_diagnose_relays_what_each_source_actually_said():
    """Tahmin yurutmek yerine kaynaklarin kendi acikladigi sebebi aktar."""
    msg = jobq.diagnose_empty_result({
        "tweets_seen": 0, "tweets_with_ca": 0, "ca_candidates": 0,
        "skipped_wrong_chain": 0, "chains_seen": {},
        "source_attempts": [
            "apify: 0 tweet — her iki girdi bicimi de bos dondu",
            "nitter: 0 tweet — 5 nitter ornegi denendi, hicbiri yanit vermedi",
        ],
    })
    assert "apify" in msg and "nitter" in msg
    assert "girdi bicimi" in msg


def test_diagnose_when_no_source_was_even_tried():
    msg = jobq.diagnose_empty_result({
        "tweets_seen": 0, "tweets_with_ca": 0, "ca_candidates": 0,
        "skipped_wrong_chain": 0, "chains_seen": {}, "source_attempts": [],
    })
    assert "TWEET_SOURCES" in msg


def test_slim_stats_carries_source_attempts():
    slim = jobq._slim_stats({"tweets_seen": 0, "source_attempts": ["apify: kapali"]})
    assert slim["source_attempts"] == ["apify: kapali"]
