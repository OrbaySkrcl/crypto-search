"""INGEST ISI: tweet topla -> CA cikar -> DEX'te dogrula -> Call olustur.

Kritik detay: tweet yeniyse (dakikalar icinde) DexScreener'dan aldigimiz ANLIK
fiyat, o cagrinin giris fiyatidir. Ucuncu taraf tarihsel API'ye hic gerek kalmaz
ve dogruluk en yuksek seviyededir. Bot ne kadar sik calisirsa veri o kadar iyi.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..config import settings
from ..db.models import IngestRun, Token, TokenStatus, utcnow
from ..db.session import session_scope
from ..http import HttpClient
from ..ingest.collector import Collector, build_sources
from ..ingest.extractor import ExtractedCA, extract_contract_addresses, looks_like_alpha_tweet
from ..oracle.dexscreener import DexScreenerClient
from ..oracle.types import TokenInfo
from . import repo

log = logging.getLogger(__name__)

# Tweet bu suredan daha yeniyse anlik fiyat T1 kabul edilir
FRESH_ENTRY_WINDOW = timedelta(minutes=12)


async def run_ingest(
    lookback_minutes: int | None = None,
    queries: list[str] | None = None,
    handles: list[str] | None = None,
    limit: int | None = None,
) -> dict:
    lookback = lookback_minutes or settings.ingest_lookback_minutes
    since = datetime.now(timezone.utc) - timedelta(minutes=lookback)
    queries = queries if queries is not None else settings.query_list
    handles = handles if handles is not None else settings.watchlist
    per_query = limit or max(50, settings.ingest_max_tweets_per_run // max(1, len(queries) or 1))

    stats = {"tweets_seen": 0, "tweets_new": 0, "calls_new": 0, "tokens_new": 0, "rejected": 0}

    async with HttpClient() as http:
        collector = Collector(build_sources(http))
        raws = await collector.collect_search(queries, since, per_query)
        if handles:
            raws += await collector.collect_timelines(handles, since, 100)

        # Tekillestir
        uniq = {r.tweet_id: r for r in raws}
        raws = list(uniq.values())
        stats["tweets_seen"] = len(raws)
        log.info("%d tekil tweet toplandi", len(raws))

        # --- 1) CA adaylarini cikar ------------------------------------- #
        chains = settings.chain_list
        per_tweet: dict[str, list[ExtractedCA]] = {}
        all_addresses: set[str] = set()
        for r in raws:
            if r.is_retweet or not looks_like_alpha_tweet(r.text):
                continue
            cas = extract_contract_addresses(r.text, chains=chains, expanded_urls=r.expanded_urls)
            if cas:
                per_tweet[r.tweet_id] = cas
                all_addresses.update(c.address for c in cas)

        if not all_addresses:
            log.info("CA iceren tweet bulunamadi")
            _record_run(stats, queries)
            return stats

        # --- 2) Bilinen adresleri DB'den ele (API tasarrufu) ------------- #
        known_info: dict[str, TokenInfo] = {}
        to_query: set[str] = set()
        with session_scope() as s:
            rows = list(s.scalars(select(Token).where(Token.address.in_(list(all_addresses)))))
            cached = {t.address: t for t in rows}
            stale_after = utcnow() - timedelta(minutes=10)
            for addr in all_addresses:
                t = cached.get(addr)
                if t is None:
                    to_query.add(addr)
                elif t.status == TokenStatus.INVALID:
                    stats["rejected"] += 1          # zaten cop olarak biliniyor
                elif t.last_refreshed_at is None or repo._aware(t.last_refreshed_at) < stale_after:
                    to_query.add(addr)

        # --- 3) DexScreener ile dogrula (zincir burada kesinlesir) ------- #
        dex = DexScreenerClient(http)
        if to_query:
            known_info = await dex.tokens(sorted(to_query))
            log.info("%d adres sorgulandi, %d dogrulandi", len(to_query), len(known_info))

        # --- 4) Kalici hale getir ---------------------------------------- #
        now = datetime.now(timezone.utc)
        touched_tokens: set[int] = set()
        with session_scope() as s:
            for r in raws:
                cas = per_tweet.get(r.tweet_id)
                if not cas:
                    continue
                account = repo.upsert_account(s, r)
                tweet, is_new = repo.upsert_tweet(s, r, account)
                if is_new:
                    stats["tweets_new"] += 1

                # Ayni adres birden fazla zincir adayi olarak gelebilir (EVM);
                # DexScreener'in dondurdugu zincir otoritedir.
                handled: set[str] = set()
                for ca in cas:
                    if ca.address in handled:
                        continue
                    info = known_info.get(ca.address.lower())
                    if info is None:
                        tok = repo.get_token(s, ca.chain, ca.address)
                        if tok is None or tok.status == TokenStatus.INVALID:
                            repo.mark_token_invalid(s, ca.chain, ca.address)
                            stats["rejected"] += 1
                            handled.add(ca.address)
                            continue
                        chain, info_obj = tok.chain, None
                    else:
                        chain, info_obj = (info.chain or ca.chain), info

                    if settings.chain_list and chain not in settings.chain_list:
                        handled.add(ca.address)
                        continue

                    existed = repo.get_token(s, chain, ca.address) is not None
                    token = repo.upsert_token(s, chain, ca.address, info_obj)
                    if not existed:
                        stats["tokens_new"] += 1
                    if info_obj:
                        repo.add_snapshot(s, token, info_obj, now)

                    call, created = repo.create_call(s, account, token, tweet, chain)
                    handled.add(ca.address)
                    if not created:
                        continue
                    stats["calls_new"] += 1
                    touched_tokens.add(token.id)

                    # --- Taze tweet: anlik fiyat = giris fiyati ---------- #
                    posted = repo._aware(tweet.posted_at)
                    if info_obj and info_obj.price_usd and (now - posted) <= FRESH_ENTRY_WINDOW:
                        call.entry_price_usd = info_obj.price_usd
                        call.entry_mc_usd = info_obj.mc_usd
                        call.entry_liquidity_usd = info_obj.liquidity_usd
                        call.entry_source = "live_at_ingest"
                        # Gecikme buyudukce guven duser
                        lag = (now - posted).total_seconds()
                        call.entry_confidence = max(0.70, 1.0 - lag / FRESH_ENTRY_WINDOW.total_seconds() * 0.30)
                        call.entry_resolved_at = now
                        from ..scoring import metrics as M
                        call.mc_earliness = M.mc_earliness(
                            call.entry_mc_usd,
                            settings.mc_earliness_low_usd,
                            settings.mc_earliness_high_usd,
                        )

            for tid in touched_tokens:
                repo.refresh_caller_ranks(s, tid)

        _record_run(stats, queries)

    log.info(
        "ingest bitti: %(tweets_seen)d goruldu / %(tweets_new)d yeni tweet / "
        "%(calls_new)d yeni cagri / %(tokens_new)d yeni token / %(rejected)d elendi", stats
    )
    return stats


def _record_run(stats: dict, queries: list[str]) -> None:
    with session_scope() as s:
        s.add(
            IngestRun(
                source=",".join(settings.source_list),
                query=" | ".join(queries)[:500],
                finished_at=utcnow(),
                tweets_seen=stats["tweets_seen"],
                tweets_new=stats["tweets_new"],
                calls_new=stats["calls_new"],
                ok=True,
            )
        )
