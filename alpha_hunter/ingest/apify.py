"""Apify aktor kaynagi -- odemeli ama en guvenilir yol.

Nitter dustugunde/veri eksik geldiginde bu devreye girer. Aktor ciktisi
aktorden aktore degistigi icin normalizer esnek yazildi.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
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



# --------------------------------------------------------------------------- #
#  Girdi bicimleri
#
#  Apify'de her Twitter aktoru farkli bir girdi semasi kullaniyor ve yanlis
#  sema sessizce [{"noResults": true}] donduruyor. Semayi tahmin etmek yerine
#  sistem DENEYEREK buluyor ve calisan bicimi hatirliyor.
# --------------------------------------------------------------------------- #
_SHAPE_KEY = "apify_input_shape"


def _d(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


INPUT_SHAPES: list[tuple[str, Any]] = [
    ("searchTerms_from", lambda kind, target, since, n: {
        "searchTerms": [f"from:{target}" if kind == "user" else target],
        "maxItems": n, "sort": "Latest",
    }),
    ("twitterHandles", lambda kind, target, since, n: {
        "twitterHandles": [target], "maxItems": n, "sort": "Latest",
    } if kind == "user" else None),
    ("searchTerms_dated", lambda kind, target, since, n: {
        "searchTerms": [f"from:{target}" if kind == "user" else target],
        "maxItems": n, "sort": "Latest",
        "start": _d(since), "end": _d(datetime.now(timezone.utc)),
    }),
    ("startUrls_obj", lambda kind, target, since, n: {
        "startUrls": [{"url": f"https://x.com/{target}"}], "maxItems": n,
    } if kind == "user" else None),
    ("startUrls_plain", lambda kind, target, since, n: {
        "startUrls": [f"https://x.com/{target}"], "maxItems": n,
    } if kind == "user" else None),
    ("queryType", lambda kind, target, since, n: {
        "searchTerms": [f"from:{target}" if kind == "user" else target],
        "maxItems": n, "queryType": "Latest",
    }),
    ("searchQueries", lambda kind, target, since, n: {
        "searchQueries": [f"from:{target}" if kind == "user" else target],
        "maxTweets": n,
    }),
    ("handles_legacy", lambda kind, target, since, n: {
        "handles": [target], "tweetsDesired": n, "mode": "own",
    } if kind == "user" else None),
]


def _remembered_shape() -> str | None:
    from ..db.models import AppState
    from ..db.session import session_scope

    try:
        with session_scope() as s:
            row = s.get(AppState, _SHAPE_KEY)
            return row.value if row else None
    except Exception:
        return None


def _remember_shape(name: str) -> None:
    from ..db.models import AppState, utcnow
    from ..db.session import session_scope

    try:
        with session_scope() as s:
            row = s.get(AppState, _SHAPE_KEY)
            if row is None:
                s.add(AppState(key=_SHAPE_KEY, value=name))
                log.info("apify girdi bicimi ogrenildi: %s", name)
            elif row.value != name:
                log.info("apify girdi bicimi degisti: %s -> %s", row.value, name)
                row.value = name
                row.updated_at = utcnow()
    except Exception:
        log.debug("apify bicimi kaydedilemedi", exc_info=True)


# Aktorun kendi bildirdigi alan adlarini bizim kavramlarimiza esler.
# Sirali: once tam eslesme, sonra anahtar kelime.
_FIELD_HINTS = {
    "query": ["searchterms", "searchqueries", "queries", "terms", "search", "keywords"],
    "handle": ["twitterhandles", "handles", "usernames", "profiles", "users", "accounts"],
    "url": ["starturls", "urls", "profileurls"],
    "limit": ["maxitems", "maxtweets", "tweetsdesired", "resultslimit", "maxresults", "limit"],
    "sort": ["sort", "querytype", "sortby"],
    "since": ["start", "since", "startdate", "fromdate"],
    "until": ["end", "until", "enddate", "todate"],
}


def _match_field(props: dict, role: str) -> str | None:
    """Semada verilen role uyan alan adini bulur."""
    names = list(props.keys())
    lowered = {n.lower(): n for n in names}
    for hint in _FIELD_HINTS.get(role, []):
        if hint in lowered:
            return lowered[hint]
    for hint in _FIELD_HINTS.get(role, []):
        for low, orig in lowered.items():
            if hint in low:
                return orig
    return None


def payload_from_schema(
    props: dict, kind: str, target: str, since: datetime, limit: int
) -> dict | None:
    """Aktorun BILDIRDIGI alan adlarindan girdi uretir.

    Tahmin yerine olcum: aktor kendi semasinda hangi alani bekledigini
    soyluyor, biz de o ada gore dolduruyoruz. Yeni bir aktore gecildiginde
    kod degistirmeye gerek kalmiyor.
    """
    if not props:
        return None
    payload: dict = {}

    q_field = _match_field(props, "query")
    h_field = _match_field(props, "handle")
    u_field = _match_field(props, "url")

    if kind == "user":
        if h_field:
            payload[h_field] = [target]
        elif q_field:
            payload[q_field] = [f"from:{target}"]
        elif u_field:
            payload[u_field] = [{"url": f"https://x.com/{target}"}]
        else:
            return None
    else:
        if q_field:
            payload[q_field] = [target]
        else:
            return None

    lim_field = _match_field(props, "limit")
    if lim_field:
        payload[lim_field] = limit

    sort_field = _match_field(props, "sort")
    if sort_field:
        enum = (props.get(sort_field) or {}).get("enum")
        payload[sort_field] = "Latest" if not enum else (
            "Latest" if "Latest" in enum else enum[0]
        )

    since_field = _match_field(props, "since")
    if since_field and (props.get(since_field) or {}).get("type") == "string":
        payload[since_field] = since.strftime("%Y-%m-%d")

    return payload or None


def _is_empty_marker(items: list) -> bool:
    """apidojo bos aramayi [{"noResults": true}] olarak bildiriyor."""
    return bool(items) and all(
        isinstance(i, dict) and (i.get("noResults") or i.get("no_results"))
        for i in items
    )


class ApifySource:
    name = "apify"

    def __init__(self, http: HttpClient, token: str | None = None, actor: str | None = None) -> None:
        self.http = http
        self.token = token or settings.apify_token
        self.actor = (actor or settings.apify_actor).replace("/", "~")
        self.last_detail: str | None = None
        self.last_run_log: str | None = None
        self.last_run_id: str | None = None
        self.actor_pricing: str | None = None
        self.actor_title: str | None = None
        self.actor_trial_days = None

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

    # ------------------------------------------------------------------ #
    async def input_schema(self) -> tuple[dict, str | None]:
        """Aktorun bildirdigi girdi semasini Apify'dan okur.

        (alanlar, hata) doner. Boylece alan adlarini tahmin etmek yerine
        aktorun kendi beyanini kullanabiliyoruz.
        """
        if not self.token:
            return ({}, "token yok")
        auth = {"Authorization": f"Bearer {self.token}"}

        info = await self.http.get(
            f"{_BASE}/acts/{self.actor}", bucket="apify",
            headers=auth, max_retries=1, timeout=30.0,
        )
        data = (info or {}).get("data") if isinstance(info, dict) else None
        if not data:
            return ({}, f"aktor bulunamadi: {self.actor.replace('~', '/')}")
        # Ucretlendirme modelini sakla -- kiralama sorunu teshiste isimize yarar
        prices = data.get("pricingInfos") or []
        if prices:
            son = prices[-1]
            self.actor_pricing = son.get("pricingModel") or "?"
            self.actor_trial_days = son.get("trialMinutes")
        self.actor_title = data.get("title") or data.get("name")

        build_id = (
            ((data.get("taggedBuilds") or {}).get("latest") or {}).get("buildId")
            or data.get("defaultRunOptions", {}).get("build")
        )
        if not build_id:
            return ({}, "aktorun yayinlanmis derlemesi yok")

        build = await self.http.get(
            f"{_BASE}/actor-builds/{build_id}", bucket="apify",
            headers=auth, max_retries=1, timeout=30.0,
        )
        raw = ((build or {}).get("data") or {}).get("inputSchema")
        if not raw:
            return ({}, "aktor girdi semasi yayinlamamis")
        try:
            import json

            schema = json.loads(raw) if isinstance(raw, str) else raw
            return (schema.get("properties") or {}, None)
        except Exception as exc:
            return ({}, f"sema okunamadi: {exc}")

    async def run_log(self, run_id: str, lines: int = 18) -> str | None:
        """Aktorun kendi calisma logunu okur.

        Aktor 'sonuc yok' dediginde SEBEBI burada yazar: proxy yok, oran
        siniri, giris gerekiyor, kiralama bitmis vb. Tahmin yurutmeye son.
        """
        if not self.token:
            return None
        text = await self.http.get(
            f"{_BASE}/actor-runs/{run_id}/log",
            bucket="apify",
            headers={"Authorization": f"Bearer {self.token}"},
            expect_json=False, max_retries=1, timeout=30.0,
        )
        if not isinstance(text, str) or not text.strip():
            return None
        rows = [r for r in text.strip().split("\n") if r.strip()]
        return "\n".join(rows[-lines:])

    async def _run(self, payload: dict, limit: int) -> list[dict]:
        """Aktoru ASENKRON calistirir: baslat -> bitmesini bekle -> sonucu al.

        Neden senkron uc (run-sync-get-dataset-items) kullanilmiyor: o uc,
        aktor isini bitirene kadar HTTP baglantisini acik tutar. Bir Twitter
        taramasi dakikalar surebilir; istemci zaman asimina ugrayinca Apify
        tarafinda is CALISMAYA DEVAM EDER ve UCRETLENDIRILIR, ama bize hicbir
        sonuc donmez. Yeniden deneme bunu ucla carpar.
        """
        self.last_detail = None
        if not self.token:
            self.last_detail = "APIFY_TOKEN tanimli degil"
            return []

        auth = {"Authorization": f"Bearer {self.token}"}
        timeout = settings.apify_request_timeout

        started = await self.http.post(
            f"{_BASE}/acts/{self.actor}/runs",
            bucket="apify", json_body=payload, headers=auth,
            max_retries=0,          # ucretli is: asla korlemesine tekrarlama
            timeout=timeout,
        )
        if not isinstance(started, dict) or not started.get("data"):
            err = (started or {}).get("error", {}) if isinstance(started, dict) else {}
            self.last_detail = (
                f"aktor baslatilamadi ({self.actor}): "
                f"{err.get('type', 'yanit yok')} {str(err.get('message', ''))[:160]}".strip()
            )
            log.error("apify: %s", self.last_detail)
            return []

        run = started["data"]
        run_id, dataset_id = run.get("id"), run.get("defaultDatasetId")
        self.last_run_id = run_id
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
                f"{_BASE}/actor-runs/{run_id}", bucket="apify", headers=auth,
                max_retries=1, timeout=timeout,
            )
            status = ((info or {}).get("data") or {}).get("status") or status

        if status != "SUCCEEDED":
            self.last_detail = f"aktor {status} durumuyla bitti"
            log.warning("apify isi %s: %s", run_id, status)
            return []

        items = await self.http.get(
            f"{_BASE}/datasets/{dataset_id}/items",
            bucket="apify",
            params={"limit": str(limit), "clean": "true", "format": "json"},
            headers=auth, max_retries=1, timeout=timeout,
        )
        if not isinstance(items, list):
            self.last_detail = "sonuc kumesi okunamadi"
            return []
        if _is_empty_marker(items) or not items:
            # Aktor "hicbir sey bulamadim" diyor. SEBEBINI kendi logunda yazar.
            self.last_run_log = await self.run_log(run_id)
            self.last_detail = "aktor sonuc bulamadi"
            if self.last_run_log:
                son = self.last_run_log.strip().split("\n")[-1][:200]
                self.last_detail += f" — aktor logu: {son}"
            return []
        return items[:limit]

    async def _run_shapes(
        self, kind: str, target: str, since: datetime, limit: int
    ) -> tuple[list[dict], str | None]:
        """Calisan girdi bicimini bulana kadar sirayla dener ve ogrenir."""
        notes: list[str] = []
        remembered = _remembered_shape()

        # 1) OLCULMUS basari her zaman once denenir. Semadan turetilen girdi
        #    bir tahmindir; calistigi kanitlanmis bicim varsa onu gecemez.
        #    Aksi halde her taramada bir fazla UCRETLI aktor calismasi olur.
        if remembered and remembered != "schema":
            build = dict(INPUT_SHAPES).get(remembered)
            payload = build(kind, target, since, limit) if build else None
            if payload:
                items = await self._run(payload, limit)
                if items:
                    return (items, remembered)
                notes.append(f"{remembered}: {self.last_detail or 'bos'}")

        # 2) Aktorun kendi bildirdigi semadan uret
        if True:
            props, schema_err = await self.input_schema()
            if props:
                payload = payload_from_schema(props, kind, target, since, limit)
                if payload:
                    items = await self._run(payload, limit)
                    if items:
                        _remember_shape("schema")
                        return (items, "schema")
                    notes.append(
                        f"schema({','.join(sorted(payload))}): {self.last_detail or 'bos'}"
                    )
            elif schema_err:
                notes.append(f"sema: {schema_err}")

        # 3) Kalan bilinen bicimleri sirayla dene, calisani hatirla
        order = [sh for sh in INPUT_SHAPES if sh[0] != remembered]
        for name, build in order:
            payload = build(kind, target, since, limit)
            if payload is None:
                continue
            items = await self._run(payload, limit)
            if items:
                _remember_shape(name)
                return (items, name)
            notes.append(f"{name}: {self.last_detail or 'bos'}")
            if self.last_detail and "baslatilamadi" in self.last_detail:
                break            # aktor/token sorunu -- digerlerini denemek bosuna
        self.last_detail = "hicbir girdi bicimi sonuc vermedi — " + "; ".join(notes[:4])
        return ([], None)

    # ------------------------------------------------------------------ #
    async def search(self, query: str, since: datetime, limit: int = 200) -> AsyncIterator[RawTweet]:
        items, _shape = await self._run_shapes("search", query, since, limit)
        got = 0
        for item in items:
            t = _normalise(item)
            if t and t.posted_at >= since:
                got += 1
                yield t
        _consume_budget(got)

    async def user_timeline(self, handle: str, since: datetime, limit: int = 100) -> AsyncIterator[RawTweet]:
        items, _shape = await self._run_shapes("user", handle, since, limit)
        got = 0
        for item in items:
            t = _normalise(item)
            if t and t.posted_at >= since:
                got += 1
                yield t
        _consume_budget(got)
        if items and not got:
            self.last_detail = (
                f"{len(items)} kayit geldi ama hicbiri son "
                f"{(datetime.now(timezone.utc) - since).days} gun icinde degil "
                "ya da bicimi cozulemedi"
            )

    # ------------------------------------------------------------------ #
    async def probe(self, handle: str = "elonmusk", per_shape: int = 10) -> list[dict]:
        """Butun girdi bicimlerini deneyip hangisinin veri dondurdugunu raporlar.

        Aktorun semasini tahmin etmek yerine olcuyoruz. Calisan bicim
        hatirlaniyor, bir daha aranmiyor.
        """
        since = datetime.now(timezone.utc) - timedelta(days=30)
        out: list[dict] = []
        winner: str | None = None
        for name, build in INPUT_SHAPES:
            payload = build("user", handle, since, per_shape)
            if payload is None:
                continue
            items = await self._run(payload, per_shape)
            parsed = sum(1 for i in items if _normalise(i))
            out.append({
                "bicim": name,
                "kayit": len(items),
                "cozulen": parsed,
                "not": None if items else (self.last_detail or "bos"),
                "gonderilen": sorted(payload.keys()),
            })
            if items and winner is None:
                winner = name
                _remember_shape(name)
                break              # calisan bulundu, gerisini deneyip para harcama
        return out

    async def diagnose(self, handle: str = "elonmusk") -> dict:
        out: dict = {
            "token_var": bool(self.token),
            "token_onek": (self.token or "")[:12] + "..." if self.token else None,
            "aktor": self.actor.replace("~", "/"),
            "gunluk_kullanim": budget_used(),
            "gunluk_butce": settings.apify_daily_tweet_budget,
            "butce_doldu": budget_exhausted(),
            "hatirlanan_bicim": _remembered_shape(),
        }
        if not self.token:
            out["sonuc"] = "APIFY_TOKEN tanimli degil"
            return out

        me = await self.http.get(
            f"{_BASE}/users/me", bucket="apify",
            headers={"Authorization": f"Bearer {self.token}"},
            max_retries=0, timeout=30.0,
        )
        if isinstance(me, dict) and me.get("data"):
            out["hesap"] = me["data"].get("username") or "?"
            out["token_gecerli"] = True
        else:
            out["token_gecerli"] = False
            out["sonuc"] = "Token gecersiz ya da Apify'a ulasilamiyor"
            return out

        props, schema_err = await self.input_schema()
        out["sema_alanlari"] = sorted(props.keys())[:30] if props else []
        out["sema_hatasi"] = schema_err
        out["aktor_adi"] = self.actor_title
        out["aktor_ucretlendirme"] = self.actor_pricing
        if props:
            # 30 gun geriden bak: "bugunden beri" arayan bir sema bos doner
            ornek = payload_from_schema(
                props, "user", handle, datetime.now(timezone.utc) - timedelta(days=30), 10
            )
            out["semadan_uretilen_girdi"] = ornek

        results = await self.probe(handle)
        out["bicim_denemeleri"] = results
        calisan = next((r for r in results if r["kayit"]), None)

        if calisan is None:
            out["kayit_sayisi"] = 0
            out["sonuc"] = "hicbir girdi bicimi sonuc vermedi"
            out["aktor_logu"] = self.last_run_log
            if self.last_run_id:
                out["apify_konsol"] = (
                    f"https://console.apify.com/actors/runs/{self.last_run_id}"
                )
            out["oneri"] = (
                f"'{self.actor.replace('~', '/')}' aktoru bu girdi bicimlerinin hicbirini "
                "kabul etmiyor. APIFY_ACTOR degiskenini baska bir aktorle degistir "
                "(ornek: apidojo/twitter-scraper-lite veya kaitoeasyapi/twitter-x-data-tweet-scraper). "
                "Aktorun Apify sayfasindaki 'Input' sekmesinde beklenen alan adlari yazar."
            )
            return out

        out["calisan_bicim"] = calisan["bicim"]
        out["kayit_sayisi"] = calisan["kayit"]
        out["cozumlenebildi"] = calisan["cozulen"] > 0
        out["sonuc"] = "calisiyor" if calisan["cozulen"] else "kayit geliyor ama cozumlenemiyor"
        return out


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


def _dig(d: dict, *path: str):
    """Ic ice sozlukten guvenli okuma: _dig(x, "core", "user_results", "result")."""
    cur: object = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _author_of(item: dict) -> dict:
    """Yazar nesnesini bilinen butun yerlerde arar.

    Aktorler X'in GraphQL yanitini bazen oldugu gibi geciriyor; o zaman yazar
    core.user_results.result.legacy altinda gomulu geliyor.
    """
    for cand in (
        item.get("author"),
        item.get("user"),
        _dig(item, "core", "user_results", "result", "legacy"),
        _dig(item, "core", "user_results", "result"),
        _dig(item, "user_results", "result", "legacy"),
        _dig(item, "tweet", "author"),
        _dig(item, "legacy", "user"),
    ):
        if isinstance(cand, dict) and cand:
            return cand
    return {}


def _handle_of(item: dict) -> str:
    author = _author_of(item)
    raw = (
        _pick(item, "username", "userName", "screen_name", "handle", default=None)
        or _pick(author, "userName", "username", "screen_name", "handle", default=None)
    )
    if not raw:
        # Son care: tweet URL'sinden cikar (x.com/<handle>/status/<id>)
        url = str(_pick(item, "url", "twitterUrl", "tweetUrl", default="") or "")
        parts = [p for p in url.split("/") if p]
        if "status" in parts:
            i = parts.index("status")
            if i > 0:
                raw = parts[i - 1]
    return str(raw or "").lstrip("@").lower()


def _why_normalise_failed(item: dict) -> str | None:
    """Cozumleme neden basarisiz oldu? Teshis ekraninda gosterilir."""
    if not isinstance(item, dict):
        return f"kayit sozluk degil ({type(item).__name__})"
    if item.get("noResults"):
        return "aktor 'noResults' isaretli bos kayit dondurdu"
    if not _handle_of(item):
        return "kullanici adi bulunamadi (author.userName / username / URL)"
    tid = _pick(item, "id", "id_str", "tweetId", "rest_id", "conversationId", default=None)
    url = _pick(item, "url", "twitterUrl", "tweetUrl", default=None)
    if not tid and not url:
        return "tweet kimligi bulunamadi (id / rest_id / url)"
    raw_dt = _pick(item, "createdAt", "created_at", "date", "timestamp", "time", default=None)
    if raw_dt is None:
        return "tarih alani bulunamadi (createdAt / created_at / date)"
    if _parse_dt(raw_dt) is None:
        return f"tarih cozulemedi: {str(raw_dt)[:40]!r}"
    return None


def _normalise(item: dict, source: str = "apify") -> RawTweet | None:
    if not isinstance(item, dict) or item.get("noResults"):
        return None

    author = _author_of(item)
    handle = _handle_of(item)

    tweet_id = str(_pick(item, "id", "id_str", "tweetId", "rest_id", default="") or "")
    url = _pick(item, "url", "twitterUrl", "tweetUrl")
    if not tweet_id and url:
        tweet_id = str(url).rstrip("/").split("/")[-1].split("?")[0]
    if not tweet_id or not handle:
        return None

    posted = _parse_dt(_pick(item, "createdAt", "created_at", "date", "timestamp", "time"))
    if posted is None:
        return None

    text = str(
        _pick(item, "fullText", "full_text", "text", "content", "rawContent", default="")
        or _dig(item, "legacy", "full_text")
        or ""
    )

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
