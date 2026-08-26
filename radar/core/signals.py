"""ADIM 5 — SINYALLER. Yalnizca UC tane var, ve hepsi olculebilir.

1. KONFLUANS  — birbirinden bagimsiz N akilli cuzdan, W saat icinde ayni
                tokende alim yapti. Botun tek "bak buraya" sinyali budur.
2. HACIM      — son 1 saat hacmi, tokenin KENDI 24 saatlik saatlik
                ortalamasinin katina cikti. Tek basina alim sebebi degil;
                konfluansin uzerine geldiginde anlamlidir.
3. TAKIP      — kullanicinin kendi listesindeki tokende fiyat esigi asildi
                ya da akilli para o tokene dokundu.

Bilinclil olarak YOK: "yapay zeka gunluk raporu", "olta listesi",
"hareketli ortalama tablosu", genel piyasa yorumu. Bunlar olculemez;
olculemeyen sey kazandirmaz, yalnizca mesgul eder.

Her aday, gonderilmeden once GUVENLIK KAPISINDAN gecer.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import Alert, AlertKind, Token, TokenStatus, Trade, Wallet, Watch, session_scope, utcnow
from ..http import HttpClient
from ..sources.safety import check_token
from . import prefs, repo

log = logging.getLogger(__name__)

SAFETY_CACHE_HOURS = 6
# Bir tarama turunda en fazla kac aday islenir (bedava kota korumasi)
MAX_CANDIDATES_PER_SCAN = 12


@dataclass(slots=True)
class Candidate:
    kind: str
    token_id: int
    dedupe_key: str
    chat_id: str | None = None
    payload: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
#  1) Konfluans
# --------------------------------------------------------------------------- #
def find_confluence(session: Session) -> list[Candidate]:
    min_wallets = int(prefs.get("esik_cuzdan"))
    smart_ids = repo.smart_wallet_ids(session)
    if len(smart_ids) < min_wallets:
        return []

    since = utcnow() - timedelta(hours=settings.confluence_window_hours)
    rows = list(
        session.execute(
            select(
                Trade.token_id,
                func.count(func.distinct(Trade.wallet_id)).label("n"),
            )
            .where(
                Trade.side == "buy",
                Trade.at >= since,
                Trade.wallet_id.in_(smart_ids),
            )
            .group_by(Trade.token_id)
            .having(func.count(func.distinct(Trade.wallet_id)) >= min_wallets)
        )
    )

    out: list[Candidate] = []
    for token_id, n_wallets in rows:
        if _recently_alerted(session, AlertKind.CONFLUENCE.value, token_id,
                             settings.confluence_cooldown_hours):
            continue
        detail = list(
            session.execute(
                select(Trade.wallet_id, func.min(Trade.at), func.sum(Trade.usd))
                .where(
                    Trade.side == "buy",
                    Trade.at >= since,
                    Trade.token_id == token_id,
                    Trade.wallet_id.in_(smart_ids),
                )
                .group_by(Trade.wallet_id)
            )
        )
        wallets = []
        total_usd = 0.0
        for wid, first_at, usd in detail:
            w = session.get(Wallet, wid)
            if w is None:
                continue
            total_usd += float(usd or 0)
            wallets.append(
                {"kisa": w.short(), "skor": w.score, "isabet": w.win_rate,
                 "n": w.n_evaluated, "ilk_alim": first_at.isoformat(), "usd": usd}
            )
        wallets.sort(key=lambda x: x["skor"] or 0, reverse=True)

        # Ayni yonde mi? Akilli cuzdanlardan satis gelmis mi diye bakiyoruz.
        n_sellers = session.scalar(
            select(func.count(func.distinct(Trade.wallet_id))).where(
                Trade.side == "sell",
                Trade.at >= since,
                Trade.token_id == token_id,
                Trade.wallet_id.in_(smart_ids),
            )
        ) or 0

        out.append(
            Candidate(
                kind=AlertKind.CONFLUENCE.value,
                token_id=token_id,
                dedupe_key=f"confluence:{token_id}:{_bucket(settings.confluence_cooldown_hours)}",
                payload={
                    "cuzdan_sayisi": int(n_wallets),
                    "satan_cuzdan": int(n_sellers),
                    "toplam_usd": round(total_usd, 2),
                    "pencere_saat": settings.confluence_window_hours,
                    "cuzdanlar": wallets[:6],
                },
            )
        )
    return out


# --------------------------------------------------------------------------- #
#  2) Sira disi hacim
# --------------------------------------------------------------------------- #
def find_volume_spikes(session: Session) -> list[Candidate]:
    out: list[Candidate] = []
    # Son bekleme suresi icinde hacim karti almis tokenleri TEK sorguda al:
    # aksi halde her turda yuzlerce token icin ayri sorgu atilir.
    cooled = _alerted_since(session, AlertKind.VOLUME.value, settings.volume_cooldown_hours)
    rows = session.scalars(
        select(Token).where(
            Token.active.is_(True),
            Token.status == TokenStatus.LIVE,
            Token.last_volume_h1.is_not(None),
            Token.last_volume_h24.is_not(None),
        )
    )
    for tok in rows:
        if tok.id in cooled:
            continue
        h1 = tok.last_volume_h1 or 0.0
        h24 = tok.last_volume_h24 or 0.0
        if h1 < settings.volume_spike_min_h1_usd or h24 <= 0:
            continue
        baseline = h24 / 24.0
        if baseline <= 0:
            continue
        ratio = h1 / baseline
        if ratio < settings.volume_spike_multiple:
            continue
        out.append(
            Candidate(
                kind=AlertKind.VOLUME.value,
                token_id=tok.id,
                dedupe_key=f"volume:{tok.id}:{_bucket(settings.volume_cooldown_hours)}",
                payload={
                    "hacim_1s": round(h1, 2),
                    "ortalama_saatlik": round(baseline, 2),
                    "kat": round(ratio, 1),
                },
            )
        )
    return out


# --------------------------------------------------------------------------- #
#  3) Takip listesi
# --------------------------------------------------------------------------- #
def find_watch_events(session: Session) -> list[Candidate]:
    out: list[Candidate] = []
    smart_ids = repo.smart_wallet_ids(session)
    now = utcnow()
    default_pct = float(prefs.get("esik_fiyat"))
    smart_cooled = _alerted_since(session, AlertKind.WATCH_SMART.value, 12)

    for w in session.scalars(select(Watch).where(Watch.muted.is_(False))):
        tok = session.get(Token, w.token_id)
        if tok is None or tok.last_price_usd is None:
            continue

        # --- fiyat esigi ---
        if w.ref_price_usd is None or w.ref_price_usd <= 0:
            w.ref_price_usd = tok.last_price_usd
            w.ref_at = now
        else:
            pct = (tok.last_price_usd / w.ref_price_usd - 1.0) * 100.0
            threshold = w.price_pct or default_pct
            if abs(pct) >= threshold:
                out.append(
                    Candidate(
                        kind=AlertKind.WATCH_PRICE.value,
                        token_id=tok.id,
                        chat_id=w.chat_id,
                        dedupe_key=f"watch_price:{w.chat_id}:{tok.id}:{int(now.timestamp() // 300)}",
                        payload={
                            "yuzde": round(pct, 1),
                            "referans": w.ref_price_usd,
                            "esik": threshold,
                            "sure_dk": int((now - (w.ref_at or now)).total_seconds() // 60),
                        },
                    )
                )
                # Referansi kaydir: ayni hareket icin tekrar tekrar bildirim yok.
                w.ref_price_usd = tok.last_price_usd
                w.ref_at = now

        # --- akilli para dokundu mu ---
        if not smart_ids:
            continue
        since = now - timedelta(hours=6)
        hits = list(
            session.execute(
                select(Trade.wallet_id, Trade.side, func.sum(Trade.usd))
                .where(
                    Trade.token_id == tok.id,
                    Trade.at >= since,
                    Trade.wallet_id.in_(smart_ids),
                )
                .group_by(Trade.wallet_id, Trade.side)
            )
        )
        if not hits:
            continue
        if tok.id in smart_cooled:
            continue
        buys = [h for h in hits if h[1] == "buy"]
        sells = [h for h in hits if h[1] == "sell"]
        out.append(
            Candidate(
                kind=AlertKind.WATCH_SMART.value,
                token_id=tok.id,
                chat_id=w.chat_id,
                dedupe_key=f"watch_smart:{w.chat_id}:{tok.id}:{_bucket(12)}",
                payload={
                    "alan": len(buys),
                    "satan": len(sells),
                    "alim_usd": round(sum(float(h[2] or 0) for h in buys), 2),
                    "satim_usd": round(sum(float(h[2] or 0) for h in sells), 2),
                },
            )
        )
    return out


# --------------------------------------------------------------------------- #
#  Kapi
# --------------------------------------------------------------------------- #
def _bucket(hours: int) -> int:
    return int(utcnow().timestamp() // max(1, hours * 3600))


def _alerted_since(session: Session, kind: str, hours: int) -> set[int]:
    """Son `hours` saatte bu tipte kart almis token kimlikleri."""
    since = utcnow() - timedelta(hours=hours)
    return set(
        session.scalars(
            select(Alert.token_id).where(Alert.kind == kind, Alert.created_at >= since)
        )
    )


def _recently_alerted(session: Session, kind: str, token_id: int, hours: int) -> bool:
    since = utcnow() - timedelta(hours=hours)
    return bool(
        session.scalar(
            select(Alert.id)
            .where(Alert.kind == kind, Alert.token_id == token_id, Alert.created_at >= since)
            .limit(1)
        )
    )


def _basic_gate(tok: Token) -> str | None:
    """Guvenlik API'sine gitmeden once ucuz elemeler."""
    liq = tok.last_liquidity_usd or 0.0
    floor = float(prefs.get("esik_likidite"))
    if liq < floor:
        return f"likidite ${liq:,.0f} < ${floor:,.0f}"
    if tok.last_mc_usd and tok.last_mc_usd > settings.alert_max_mc_usd:
        return "piyasa degeri tavanin ustunde"
    born = tok.pool_created_at or tok.first_seen_at
    if born:
        age_min = (utcnow() - born).total_seconds() / 60
        if age_min < settings.alert_min_token_age_minutes:
            return "havuz cok yeni"
    if tok.status != TokenStatus.LIVE:
        return f"token durumu: {tok.status}"
    return None


async def _safety_gate(http: HttpClient, token_id: int) -> tuple[bool, float, list[str]]:
    """Guvenlik raporu (onbellekli). (gecti_mi, skor, bayraklar)"""
    with session_scope() as s:
        tok = s.get(Token, token_id)
        if tok is None:
            return False, 0.0, ["token bulunamadi"]
        fresh = (
            tok.safety_checked_at
            and (utcnow() - tok.safety_checked_at) < timedelta(hours=SAFETY_CACHE_HOURS)
        )
        if fresh:
            return (tok.safety_score or 0) >= settings.alert_min_safety_score, tok.safety_score or 0, tok.flags()
        chain, address = tok.chain, tok.address
        liq, mc = tok.last_liquidity_usd, tok.last_mc_usd
        born = tok.pool_created_at or tok.first_seen_at
        vol24 = tok.last_volume_h24

    age_min = (utcnow() - born).total_seconds() / 60 if born else None
    rep = await check_token(
        http, chain, address,
        liquidity_usd=liq, mc_usd=mc, age_minutes=age_min, volume_h24=vol24,
    )
    with session_scope() as s:
        tok = s.get(Token, token_id)
        if tok is not None:
            repo.set_safety(s, tok, rep.score, rep.flags)
    passed = (not rep.fatal) and rep.score >= settings.alert_min_safety_score
    return passed, rep.score, rep.flags


# --------------------------------------------------------------------------- #
#  Dis kapi
# --------------------------------------------------------------------------- #
async def scan(http: HttpClient) -> list[int]:
    """Bir tarama turu. Olusturulan alarm kimliklerini doner."""
    with session_scope() as s:
        cands = find_confluence(s) + find_watch_events(s) + find_volume_spikes(s)

    # Ilk kosuda yuzlerce aday cikabilir. Guvenlik API'leri bedava katmanda
    # saniyede yarim istek kaldiriyor; tur basina tavan koyuyoruz. Siralama
    # zaten onceliklidir: once konfluans, sonra takip, en son hacim.
    if len(cands) > MAX_CANDIDATES_PER_SCAN:
        log.info("aday tavani: %s adaydan %s tanesi bu turda islenecek",
                 len(cands), MAX_CANDIDATES_PER_SCAN)
        cands = cands[:MAX_CANDIDATES_PER_SCAN]

    created: list[int] = []
    for c in cands:
        with session_scope() as s:
            tok = s.get(Token, c.token_id)
            if tok is None:
                continue
            # Takip listesi kartlari kullanicinin kendi tokeni: temel kapiyi
            # uygulariz ama guvenlik skoru yuzunden SUSTURMAYIZ; kullanici
            # zaten o tokeni biliyor, karari kendisi verir.
            is_watch = c.kind in (AlertKind.WATCH_PRICE.value, AlertKind.WATCH_SMART.value)
            reason = _basic_gate(tok) if not is_watch else None
            if reason:
                log.debug("aday elendi %s (%s): %s", tok.symbol, c.kind, reason)
                continue
            snapshot = {
                "price": tok.last_price_usd,
                "mc": tok.last_mc_usd,
                "liq": tok.last_liquidity_usd,
            }

        if not is_watch:
            ok, score, flags = await _safety_gate(http, c.token_id)
            c.payload["guvenlik_skoru"] = round(score, 2)
            c.payload["guvenlik_bayraklari"] = flags[:5]
            if not ok:
                log.info("guvenlik kapisi elendi: token=%s skor=%.2f", c.token_id, score)
                continue

        with session_scope() as s:
            if s.scalar(select(Alert.id).where(Alert.dedupe_key == c.dedupe_key)):
                continue
            al = Alert(
                kind=c.kind,
                token_id=c.token_id,
                chat_id=c.chat_id,
                dedupe_key=c.dedupe_key,
                entry_price_usd=snapshot["price"],
                entry_mc_usd=snapshot["mc"],
                entry_liquidity_usd=snapshot["liq"],
                payload=json.dumps(c.payload, ensure_ascii=False, default=str),
            )
            s.add(al)
            s.flush()
            created.append(al.id)

    return created
