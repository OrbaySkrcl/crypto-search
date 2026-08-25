"""Apify aktor kaynagi -- odemeli ama en guvenilir yol.

Nitter dustugunde/veri eksik geldiginde bu devreye girer. Aktor ciktisi
aktorden aktore degistigi icin normalizer esnek yazildi.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from ..config import settings
from ..http import HttpClient
from .base import RawTweet

log = logging.getLogger(__name__)
_BASE = "https://api.apify.com/v2"


def _budget_key() -> str:
    return f"apify_usage_{datetime.now(timezone.utc):%Y-%m-%d}"


def budget_used() -> int:
    """Bugun Apify'dan kac tweet cekildi."""
    from ..db.models import AppState
    from ..db.session import session_scope

    try:
        with session_scope() as s:
            row = s.get(AppState, _budget_key())
            return int(row.value) if row and row.value else 0
    except Exception:
        return 0


def budget_exhausted() -> bool:
    cap = settings.apify_daily_tweet_budget
    return cap > 0 and budget_used() >= cap


def _consume_budget(n: int) -> None:
    if n <= 0 or settings.apify_daily_tweet_budget <= 0:
        return
    from ..db.models import AppState, utcnow
    from ..db.session import session_scope

    try:
        with session_scope() as s:
            key = _budget_key()
            row = s.get(AppState, key)
            if row is None:
                s.add(AppState(key=key, value=str(n)))
                total = n
            else:
                total = int(row.value or 0) + n
                row.value = str(total)
                row.updated_at = utcnow()
        cap = settings.apify_daily_tweet_budget
        if total >= cap:
            log.warning("gunluk Apify butcesi doldu: %d/%d tweet", total, cap)
        elif total >= cap * 0.8:
            log.info("Apify gunluk kullanim: %d/%d tweet", total, cap)
    except Exception:
        log.debug("apify butce sayaci guncellenemedi", exc_info=True)


class ApifySource:
    name = "apify"

    def __init__(self, http: HttpClient, token: str | None = None, actor: str | None = None) -> None:
        self.http = http
        self.token = token or settings.apify_token
        self.actor = (actor or settings.apify_actor).replace("/", "~")
        self.last_detail: str | None = None

    async def available(self) -> bool:
        if not self.token:
            self.last_detail = "APIFY_TOKEN tanimli degil"
            return False
        if budget_exhausted():
            self.last_detail = (
                f"gunluk Apify butcesi doldu ({settings.apify_daily_tweet_budget} tweet). "
                "APIFY_DAILY_TWEET_BUDGET ile artirabilirsin."
            )
            return False
        return True

    async def _run(self, payload: dict, limit: int) -> list[dict]:
        """Aktoru ASENKRON calistirir: baslat -> bitmesini bekle -> sonucu al.

        Neden senkron uc (run-sync-get-dataset-items) kullanilmiyor:
        o uc, aktor isini bitirene kadar HTTP baglantisini acik tutar. Bir
        Twitter taramasi dakikalar surebilir; istemci zaman asimina ugrayinca
        Apify tarafinda is CALISMAYA DEVAM EDER ve UCRETLENDIRILIR, ama bize
        hicbir sonuc donmez. Yeniden deneme bunu ucla carpar. Bu desende ise
        baglanti kisa tutuluyor, is durumu yoklanarak bekleniyor.
        """
        self.last_detail = None
        if not self.token:
            self.last_detail = "APIFY_TOKEN tanimli degil"
            return []

        auth = {"Authorization": f"Bearer {self.token}"}
        timeout = settings.apify_request_timeout

        # --- 1) isi baslat ------------------------------------------------ #
        started = await self.http.post(
            f"{_BASE}/acts/{self.actor}/runs",
            bucket="apify",
            json_body=payload,
            headers=auth,
            max_retries=0,          # ucretli is: asla korlemesine tekrarlama
            timeout=timeout,
        )
        if not isinstance(started, dict) or not started.get("data"):
            err = (started or {}).get("error", {}) if isinstance(started, dict) else {}
            self.last_detail = (
                f"aktor baslatilamadi ({self.actor}): "
                f"{err.get('type', 'yanit yok')} {str(err.get('message', ''))[:160]}".strip()
                or f"aktor baslatilamadi ({self.actor}) — token gecerli mi, aktor kiralandi mi?"
            )
            log.error("apify: %s", self.last_detail)
            return []

        run = started["data"]
        run_id = run.get("id")
        dataset_id = run.get("defaultDatasetId")
        log.info("apify isi basladi: %s (aktor %s)", run_id, self.actor)

        # --- 2) bitmesini bekle ------------------------------------------- #
        deadline = time.monotonic() + settings.apify_max_wait_seconds
        status = run.get("status", "READY")
        while status in ("READY", "RUNNING"):
            if time.monotonic() >= deadline:
                self.last_detail = (
                    f"aktor {settings.apify_max_wait_seconds}sn icinde bitmedi "
                    "(APIFY_MAX_WAIT_SECONDS ile artirabilirsin)"
                )
                log.warning("apify: %s", self.last_detail)
                return []
            await asyncio.sleep(5)
            info = await self.http.get(
                f"{_BASE}/actor-runs/{run_id}",
                bucket="apify",
                headers=auth,
                max_retries=1,
                timeout=timeout,
            )
            status = ((info or {}).get("data") or {}).get("status") or status

        if status != "SUCCEEDED":
            self.last_detail = f"aktor {status} durumuyla bitti"
            log.warning("apify isi %s: %s", run_id, status)
            if status not in ("FAILED", "ABORTED", "TIMED-OUT"):
                return []

        # --- 3) sonuclari al ---------------------------------------------- #
        items = await self.http.get(
            f"{_BASE}/datasets/{dataset_id}/items",
            bucket="apify",
            params={"limit": str(limit), "clean": "true", "format": "json"},
            headers=auth,
            max_retries=1,
            timeout=timeout,
        )
        if not isinstance(items, list):
            self.last_detail = "sonuc kumesi okunamadi"
            log.error("apify veri kumesi okunamadi: %s", dataset_id)
            return []
        if not items:
            self.last_detail = (
                f"aktor calisti ({status}) ama 0 kayit dondurdu — kredi bitmis, "
                "hesap korumali/askida ya da tarih araligi bos olabilir"
            )
            log.warning("apify: %s", self.last_detail)
        else:
            log.info("apify %d kayit dondurdu", len(items))
        return items[:limit]

    async def diagnose(self, handle: str = "elonmusk") -> dict:
        """Tek bir gercek cagri yapip ham sonucu dondurur.

        Panodaki 'Baglanti testi' dugmesi bunu cagirir: tahmin yurutmek yerine
        Apify'in ne dedigini oldugu gibi gosterir.
        """
        out: dict = {
            "token_var": bool(self.token),
            "token_onek": (self.token or "")[:12] + "..." if self.token else None,
            "aktor": self.actor.replace("~", "/"),
            "gunluk_kullanim": budget_used(),
            "gunluk_butce": settings.apify_daily_tweet_budget,
            "butce_doldu": budget_exhausted(),
        }
        if not self.token:
            out["sonuc"] = "APIFY_TOKEN tanimli degil"
            return out

        me = await self.http.get(
            f"{_BASE}/users/me",
            bucket="apify",
            headers={"Authorization": f"Bearer {self.token}"},
            max_retries=0,
            timeout=30.0,
        )
        if isinstance(me, dict) and me.get("data"):
            out["hesap"] = me["data"].get("username") or "?"
            out["token_gecerli"] = True
        else:
            out["token_gecerli"] = False
            out["sonuc"] = "Token gecersiz ya da Apify'a ulasilamiyor"
            return out

        payload = {
            "searchTerms": [f"from:{handle}"],
            "maxItems": 5,
            "sort": "Latest",
        }
        out["gonderilen_girdi"] = payload
        items = await self._run(payload, 5)
        out["kayit_sayisi"] = len(items)
        out["aciklama"] = self.last_detail
        if items:
            out["ornek_alanlar"] = sorted(items[0].keys())[:25]
            t = _normalise(items[0])
            out["cozumlenebildi"] = t is not None
            if t:
                out["ornek_tweet"] = {
                    "hesap": t.handle, "tarih": t.posted_at.isoformat(),
                    "metin": t.text[:120],
                }
        out["sonuc"] = "calisiyor" if items else "aktor sonuc dondurmedi"
        return out

    async def search(self, query: str, since: datetime, limit: int = 200) -> AsyncIterator[RawTweet]:
        payload = {
            "searchTerms": [query],
            "maxItems": limit,
            "sort": "Latest",
            "start": since.strftime("%Y-%m-%d_%H:%M:%S_UTC"),
            "includeSearchTerms": False,
        }
        items = await self._run(payload, limit)
        got = 0
        for item in items:
            t = _normalise(item)
            if t and t.posted_at >= since:
                got += 1
                yield t
        _consume_budget(got)

    async def user_timeline(self, handle: str, since: datetime, limit: int = 100) -> AsyncIterator[RawTweet]:
        """Once twitterHandles, olmazsa 'from:' aramasi.

        Aktorden aktore girdi semasi degisiyor: bazilari `twitterHandles`
        anlamiyor, hepsi `searchTerms` anliyor. Tek bicime guvenmek, hesabin
        gercekten tweet atmis olmasina ragmen bos donmesine yol aciyordu.
        """
        attempts = [
            ("twitterHandles", {
                "twitterHandles": [handle],
                "maxItems": limit,
                "sort": "Latest",
                "start": since.strftime("%Y-%m-%d"),
            }),
            ("searchTerms from:", {
                "searchTerms": [f"from:{handle}"],
                "maxItems": limit,
                "sort": "Latest",
                "start": since.strftime("%Y-%m-%d"),
            }),
        ]
        for label, payload in attempts:
            items = await self._run(payload, limit)
            if not items:
                log.info("apify @%s: '%s' bicimi bos dondu", handle, label)
                continue
            yielded = 0
            for item in items:
                t = _normalise(item)
                if t and t.posted_at >= since:
                    yielded += 1
                    yield t
            if yielded:
                self.last_detail = None
                _consume_budget(yielded)
                return
            self.last_detail = (
                f"{len(items)} kayit geldi ama hicbiri son {(datetime.now(timezone.utc) - since).days} "
                "gun icinde degil ya da bicimi taninmadi"
            )
        if not self.last_detail:
            self.last_detail = (
                "her iki girdi bicimi de bos dondu — aktor adi yanlis, kredi bitmis "
                "ya da hesap korumali/askida olabilir"
            )


# --------------------------------------------------------------------------- #
def _pick(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / (1000 if v > 1e11 else 1), tz=timezone.utc)
    s = str(v).strip()
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc) if "%z" not in fmt \
                else datetime.strptime(s, fmt).astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _normalise(item: dict, source: str = "apify") -> RawTweet | None:
    if not isinstance(item, dict) or item.get("noResults"):
        return None

    author = item.get("author") or item.get("user") or {}
    handle = str(
        _pick(item, "username", "userName", "screen_name", default=None)
        or _pick(author, "userName", "username", "screen_name", default="")
    ).lstrip("@").lower()

    tweet_id = str(_pick(item, "id", "id_str", "tweetId", "rest_id", default="") or "")
    url = _pick(item, "url", "twitterUrl", "tweetUrl")
    if not tweet_id and url:
        tweet_id = str(url).rstrip("/").split("/")[-1].split("?")[0]
    if not tweet_id or not handle:
        return None

    posted = _parse_dt(_pick(item, "createdAt", "created_at", "date", "timestamp"))
    if posted is None:
        return None

    text = str(_pick(item, "fullText", "full_text", "text", "content", default="") or "")

    urls: list[str] = []
    ent = item.get("entities") or {}
    for u in (ent.get("urls") or []):
        if isinstance(u, dict) and u.get("expanded_url"):
            urls.append(u["expanded_url"])
    for u in (item.get("urls") or []):
        if isinstance(u, str):
            urls.append(u)
        elif isinstance(u, dict) and u.get("expanded_url"):
            urls.append(u["expanded_url"])

    return RawTweet(
        tweet_id=tweet_id,
        handle=handle,
        posted_at=posted,
        text=text,
        url=str(url) if url else f"https://x.com/{handle}/status/{tweet_id}",
        source=source,
        lang=_pick(item, "lang", "language"),
        platform_user_id=str(_pick(author, "id", "id_str", "rest_id", default="") or "") or None,
        display_name=_pick(author, "name", "displayName"),
        followers=_pick(author, "followers", "followersCount", "followers_count"),
        following=_pick(author, "following", "followingCount", "friends_count"),
        account_created_at=_parse_dt(_pick(author, "createdAt", "created_at")),
        like_count=_pick(item, "likeCount", "favorite_count", "favoriteCount"),
        retweet_count=_pick(item, "retweetCount", "retweet_count"),
        reply_count=_pick(item, "replyCount", "reply_count"),
        view_count=_pick(item, "viewCount", "views"),
        is_retweet=bool(_pick(item, "isRetweet", "retweeted", default=False)),
        is_reply=bool(_pick(item, "isReply", default=False)) or bool(item.get("inReplyToId")),
        is_quote=bool(_pick(item, "isQuote", "is_quote_status", default=False)),
        expanded_urls=list(dict.fromkeys(urls)),
        raw=None,
    )
