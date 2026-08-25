"""SCORE ISI: skorlari yeniden hesaplar + koordineli hesap kumelerini bulur."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func, select

from ..config import settings
from ..db.models import Account, AccountCluster, Call, utcnow
from ..db.session import session_scope
from ..scoring.engine import ScoreResult, score_all
from ..scoring.metrics import jaccard
from . import repo

log = logging.getLogger(__name__)


def run_scoring(min_calls: int = 1) -> list[ScoreResult]:
    with session_scope() as s:
        results = score_all(s, min_calls=min_calls)
    log.info("skorlama tamam: %d hesap", len(results))
    return results


# --------------------------------------------------------------------------- #
#  Koordinasyon / promo agi tespiti
# --------------------------------------------------------------------------- #
def detect_clusters(
    min_shared: int = 3, jaccard_threshold: float = 0.5, max_delta_minutes: int = 45
) -> int:
    """Ayni tokenlari, ayni dakikalarda paylasan hesaplar bir promo agidir.

    Bunlar tek tek 'basarili' gorunebilir ama ayni pump'i besledikleri icin
    bagimsiz sinyal degildirler. Kumelenip isaretlenirler.
    """
    with session_scope() as s:
        since = utcnow() - timedelta(days=settings.score_window_days)
        rows = list(
            s.execute(
                select(Call.account_id, Call.token_id, Call.called_at).where(Call.called_at >= since)
            )
        )
        if not rows:
            return 0

        tokens_by_acc: dict[int, set[int]] = defaultdict(set)
        times: dict[tuple[int, int], list] = defaultdict(list)
        for acc_id, tok_id, called_at in rows:
            tokens_by_acc[acc_id].add(tok_id)
            times[(acc_id, tok_id)].append(repo._aware(called_at))

        # Yalnizca yeterince aktif hesaplar
        active = {a: t for a, t in tokens_by_acc.items() if len(t) >= min_shared}
        if len(active) < 2:
            return 0
        # Cok buyurse en aktif 400 hesapla sinirla (O(n^2))
        ordered = sorted(active.items(), key=lambda kv: -len(kv[1]))[:400]

        parent: dict[int, int] = {a: a for a, _ in ordered}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        pair_scores: dict[tuple[int, int], float] = {}
        for i in range(len(ordered)):
            a_id, a_tokens = ordered[i]
            for j in range(i + 1, len(ordered)):
                b_id, b_tokens = ordered[j]
                shared = a_tokens & b_tokens
                if len(shared) < min_shared:
                    continue
                jc = jaccard(a_tokens, b_tokens)
                if jc < jaccard_threshold:
                    continue
                # Zamansal yakinlik: ortak tokenlarda medyan gecikme
                deltas = []
                for tok in shared:
                    ta = min(times[(a_id, tok)])
                    tb = min(times[(b_id, tok)])
                    deltas.append(abs((ta - tb).total_seconds()) / 60.0)
                deltas.sort()
                median_delta = deltas[len(deltas) // 2]
                if median_delta <= max_delta_minutes:
                    union(a_id, b_id)
                    pair_scores[(a_id, b_id)] = jc

        groups: dict[int, list[int]] = defaultdict(list)
        for a, _ in ordered:
            groups[find(a)].append(a)

        # Onceki kume atamalarini temizle
        for acc in s.scalars(select(Account).where(Account.cluster_id.isnot(None))):
            acc.cluster_id = None
        s.execute(AccountCluster.__table__.delete())
        s.flush()

        made = 0
        for members in groups.values():
            if len(members) < 2:
                continue
            rel = [v for (a, b), v in pair_scores.items() if a in members and b in members]
            cohesion = sum(rel) / len(rel) if rel else 0.0
            handles = [
                h for h in s.scalars(select(Account.handle).where(Account.id.in_(members)))
            ]
            cluster = AccountCluster(
                label=f"agent-{made+1}: " + ", ".join(f"@{h}" for h in handles[:4]),
                member_count=len(members),
                cohesion=cohesion,
                notes={"handles": handles},
            )
            s.add(cluster)
            s.flush()
            for m in members:
                acc = s.get(Account, m)
                if acc:
                    acc.cluster_id = cluster.id
            made += 1

        log.info("%d koordineli hesap kumesi bulundu", made)
        return made


def blacklist_spammers(threshold_per_day: float | None = None) -> int:
    """KURAL 2'nin sert ucu: gunde N'den fazla tekil CA atan hesap otomatik elenir."""
    limit = threshold_per_day or settings.spray_hard_calls_per_day
    flagged = 0
    with session_scope() as s:
        since = utcnow() - timedelta(days=30)
        rows = list(
            s.execute(
                select(
                    Call.account_id,
                    func.count(func.distinct(Call.token_id)),
                    func.min(Call.called_at),
                )
                .where(Call.called_at >= since)
                .group_by(Call.account_id)
            )
        )
        for acc_id, n_tokens, first in rows:
            days = max(1.0, (utcnow() - repo._aware(first)).total_seconds() / 86400.0)
            cpd = n_tokens / days
            if cpd < limit or n_tokens < 20:
                continue
            acc = s.get(Account, acc_id)
            if acc and not acc.is_blacklisted:
                acc.is_blacklisted = True
                acc.blacklist_reason = f"spray&pray: gunde {cpd:.1f} tekil CA"
                flagged += 1
    if flagged:
        log.info("%d hesap spam nedeniyle kara listeye alindi", flagged)
    return flagged
