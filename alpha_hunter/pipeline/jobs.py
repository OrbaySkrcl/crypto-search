"""Arka plan is kuyrugu -- manuel hesap taramasi icin.

Bir backfill dakikalar surer (ucretsiz fiyat API'si yavas). Ne web istegi ne de
Telegram mesaji o kadar bekleyemez, o yuzden her iki arayuz de buraya bir is
birakir; zamanlayicidaki isci teker teker calistirir ve sonucu geri bildirir.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db.models import Job, JobStatus, utcnow
from ..db.session import session_scope

log = logging.getLogger(__name__)

MAX_DAYS = 365
MIN_DAYS = 1
# Ayni hesap icin bu sure icinde tekrar is acilmaz (kotaya yazik olmasin)
DEDUPE_WINDOW = timedelta(minutes=10)


def normalise_handle(raw: str) -> str | None:
    """@ve URL bicimlerini sade handle'a cevirir; gecersizse None."""
    if not raw:
        return None
    h = raw.strip()
    for pre in ("https://", "http://"):
        if h.startswith(pre):
            h = h[len(pre):]
    for host in ("x.com/", "twitter.com/", "www.x.com/", "www.twitter.com/"):
        if h.lower().startswith(host):
            h = h[len(host):]
    h = h.split("?")[0].split("/")[0].lstrip("@").strip().lower()
    if not h or len(h) > 15:
        return None
    if not all(c.isalnum() or c == "_" for c in h):
        return None
    return h


def clamp_days(days: int | None) -> int:
    try:
        d = int(days if days is not None else 60)
    except (TypeError, ValueError):
        d = 60
    return max(MIN_DAYS, min(MAX_DAYS, d))


def enqueue_token_profile(
    address: str, chain: str | None = None, source: str = "web",
    notify_chat_id: str | None = None,
) -> tuple[Job | None, str]:
    """Bir tokenin erken alicilarini cuzdan cagrilarina cevirmek icin is acar."""
    addr = (address or "").strip()
    if len(addr) < 30:
        return (None, "gecerli bir kontrat adresi gir")
    chain = (chain or settings.chain_list[0]).lower()

    with session_scope() as s:
        active = s.scalar(
            select(Job).where(
                Job.kind == "profile_token", Job.target == addr,
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
        )
        if active:
            return (active, f"{addr[:10]}... zaten kuyrukta (#{active.id})")
        job = Job(
            kind="profile_token", target=addr, params={"chain": chain},
            source=source, status=JobStatus.QUEUED, notify_chat_id=notify_chat_id,
        )
        s.add(job)
        s.flush()
        s.expunge(job)
        return (job, f"{addr[:10]}... erken alicilari icin kuyruga alindi — is #{job.id}")


def enqueue_backfill(
    handle: str,
    days: int = 60,
    source: str = "web",
    notify_chat_id: str | None = None,
) -> tuple[Job | None, str]:
    """Kuyruga bir tarama isi birakir. (is, mesaj) doner."""
    h = normalise_handle(handle)
    if not h:
        return (None, "gecersiz hesap adi")
    d = clamp_days(days)

    with session_scope() as s:
        active = s.scalar(
            select(Job).where(
                Job.kind == "backfill",
                Job.target == h,
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
        )
        if active:
            return (active, f"@{h} zaten kuyrukta (#{active.id})")

        recent = s.scalar(
            select(Job)
            .where(
                Job.kind == "backfill",
                Job.target == h,
                Job.created_at >= utcnow() - DEDUPE_WINDOW,
            )
            .order_by(Job.created_at.desc())
        )
        if recent:
            return (recent, f"@{h} az once tarandi (#{recent.id}) — sonucu asagida")

        job = Job(
            kind="backfill",
            target=h,
            params={"days": d},
            source=source,
            status=JobStatus.QUEUED,
            notify_chat_id=notify_chat_id,
        )
        s.add(job)
        s.flush()
        s.expunge(job)
        log.info("tarama kuyruga alindi: @%s (%d gun) #%d", h, d, job.id)
        return (job, f"@{h} kuyruga alindi ({d} gun) — is #{job.id}")


def claim_next(session: Session) -> Job | None:
    """Siradaki bekleyen isi alir ve RUNNING isaretler."""
    job = session.scalar(
        select(Job)
        .where(Job.status == JobStatus.QUEUED)
        .order_by(Job.created_at.asc())
        .limit(1)
    )
    if job is None:
        return None
    job.status = JobStatus.RUNNING
    job.started_at = utcnow()
    job.progress = "basliyor"
    session.flush()
    return job


def set_progress(job_id: int, text: str) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job:
            job.progress = text[:250]


def finish(job_id: int, result: dict | None = None, error: str | None = None) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        job.status = JobStatus.FAILED if error else JobStatus.DONE
        job.result = result
        job.error = error[:2000] if error else None
        job.progress = "hata" if error else "bitti"
        job.finished_at = utcnow()


def job_to_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "kind": job.kind,
        "target": job.target,
        "days": (job.params or {}).get("days"),
        "chain": (job.params or {}).get("chain"),
        "source": job.source,
        "status": job.status.value if hasattr(job.status, "value") else str(job.status),
        "progress": job.progress,
        "result": job.result,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def list_jobs(session: Session, limit: int = 20) -> list[dict]:
    rows = session.scalars(select(Job).order_by(Job.created_at.desc()).limit(limit))
    return [job_to_dict(j) for j in rows]


def _slim_stats(stats: dict) -> dict:
    keep = (
        "tweets_seen", "tweets_with_ca", "ca_candidates",
        "calls_new", "skipped_wrong_chain", "chains_seen", "source_attempts",
    )
    return {k: stats.get(k) for k in keep if k in stats}


def diagnose_empty_result(stats: dict) -> str:
    """Bos sonucun GERCEK sebebini soyler.

    "CA iceren tweet yok" uc farkli durumu ortuyordu ve ucunun cozumu de farkli.
    Kullaniciyi yanlis yone gondermemek icin ayirmak sart.
    """
    from ..config import settings

    seen = stats.get("tweets_seen", 0) or 0
    with_ca = stats.get("tweets_with_ca", 0) or 0
    skipped = stats.get("skipped_wrong_chain", 0) or 0
    chains_seen = stats.get("chains_seen") or {}

    if skipped:
        others = {k: v for k, v in chains_seen.items() if k not in settings.chain_list}
        names = ", ".join(sorted(others)) or "baska zincirler"
        return (
            f"{skipped} kontrat bulundu ama {names} zincirinde — sen yalnizca "
            f"{','.join(settings.chain_list)} takip ediyorsun. "
            f"CHAINS degiskenine {names} ekleyip tekrar dene."
        )
    if seen == 0:
        # Kaynaklarin kendi acikladigi sebepleri aynen aktar -- tahmin yurutmeyelim
        attempts = stats.get("source_attempts") or []
        if attempts:
            return "Hic tweet cekilemedi. Kaynaklar ne dedi: " + " | ".join(attempts[:4])
        return (
            "Bu hesaptan hic tweet cekilemedi ve hicbir kaynak denenmedi. "
            "TWEET_SOURCES bos ya da gecersiz olabilir."
        )
    if with_ca == 0:
        return f"{seen} tweet tarandi, hicbirinde kontrat adresi yok."
    return (
        f"{seen} tweette {stats.get('ca_candidates', 0)} adres adayi bulundu ama "
        "hicbiri DEX'te dogrulanamadi (silinmis token ya da yanlis pozitif olabilir)."
    )


async def run_token_profile_job(job_id: int) -> None:
    """Kosan bir tokenin erken alicilarini cikarir."""
    from ..onchain.profiler import profile_token

    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        address = job.target
        chain = (job.params or {}).get("chain") or settings.chain_list[0]

    set_progress(job_id, f"{address[:10]}... erken alicilari cikariliyor")
    try:
        stats = await profile_token(chain, address)
    except Exception as exc:
        log.exception("token profillenemedi: %s", address)
        finish(job_id, error=f"{type(exc).__name__}: {exc}")
        return

    finish(job_id, result={
        "kind": "profile_token", "address": address, "chain": chain,
        "found": bool(stats.get("yeni_cagri")),
        "wallets": stats.get("cuzdan", 0),
        "new_calls": stats.get("yeni_cagri", 0),
        "trades": stats.get("alim", 0),
        "skipped_late": stats.get("elenen", 0),
        "reason": stats.get("not"),
    })


async def run_job(job_id: int) -> None:
    """Is turune gore dogru calistiriciyi secer."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        kind = job.kind if job else None
    if kind == "profile_token":
        await run_token_profile_job(job_id)
    else:
        await run_backfill_job(job_id)


async def run_backfill_job(job_id: int) -> None:
    """Bir tarama isini bastan sona calistirir."""
    from .backfill import backfill_account

    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        handle = job.target
        days = clamp_days((job.params or {}).get("days"))

    log.info("tarama basliyor: @%s (%d gun)", handle, days)
    set_progress(job_id, f"@{handle} son {days} gun taraniyor")
    try:
        res, stats = await backfill_account(handle, days=days)
    except Exception as exc:
        log.exception("tarama basarisiz: @%s", handle)
        finish(job_id, error=f"{type(exc).__name__}: {exc}")
        return

    if res is None:
        finish(
            job_id,
            result={
                "handle": handle,
                "days": days,
                "found": False,
                "reason": diagnose_empty_result(stats),
                "stats": _slim_stats(stats),
            },
        )
        return

    finish(
        job_id,
        result={
            "stats": _slim_stats(stats),
            "handle": res.handle,
            "days": days,
            "found": True,
            "alpha_score": round(res.alpha_score, 1),
            "tier": res.tier,
            "n_calls": res.n_calls,
            "n_evaluated": res.n_evaluated,
            "n_wins": res.n_wins,
            "win_rate": round(res.win_rate, 3),
            "median_multiple": round(res.median_multiple, 2),
            "entry_quality": round(res.entry_quality, 3),
            "originality": round(res.originality, 3),
            "calls_per_day": round(res.calls_per_day, 2),
        },
    )
    log.info("tarama bitti: @%s alfa %.1f (%s)", res.handle, res.alpha_score, res.tier)
