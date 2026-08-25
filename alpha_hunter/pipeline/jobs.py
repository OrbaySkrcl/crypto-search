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
        res = await backfill_account(handle, days=days)
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
                "note": "bu hesapta kontrat adresi iceren tweet bulunamadi",
            },
        )
        return

    finish(
        job_id,
        result={
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
