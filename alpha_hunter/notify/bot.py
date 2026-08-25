"""Telegram komut botu.

Uzun yoklama (long polling) ile calisir -- webhook, alan adi veya sertifika
gerektirmez, Railway'de oldugu gibi ayaga kalkar.

GUVENLIK: yalnizca TELEGRAM_CHAT_ID'de tanimli sohbet komut verebilir.
Botun kullanici adini bulan baskasi tarama kuyruguna is birakamaz.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from ..config import settings
from ..db.models import Account, AppState, Call, Job, Token, utcnow
from ..db.session import session_scope
from ..http import HttpClient
from ..pipeline import jobs as jobq
from ..scoring.engine import latest_scores
from .telegram import TIER_EMOJI, TelegramNotifier, esc, fmt_mult, fmt_usd

log = logging.getLogger(__name__)

_OFFSET_KEY = "telegram_update_offset"

# Telegram'da '/' yazinca cikan kisayol menusu
COMMANDS: list[tuple[str, str]] = [
    ("top", "En yuksek alfa skorlu hesaplar"),
    ("son", "Son 24 saatteki cagrilar"),
    ("tara", "Bir hesabi gecmise donuk tara: /tara handle 60"),
    ("hesap", "Bir hesabin detayi: /hesap handle"),
    ("token", "Bir kontrat adresini incele: /token <CA>"),
    ("isler", "Tarama isleri ve durumlari"),
    ("durum", "Sistem durumu"),
    ("yardim", "Butun komutlar"),
]

MAIN_KEYBOARD = [
    [{"text": "🏆 Liderlik", "callback_data": "top"},
     {"text": "⚡ Son cagrilar", "callback_data": "son"}],
    [{"text": "📊 Durum", "callback_data": "durum"},
     {"text": "🔎 Isler", "callback_data": "isler"}],
    [{"text": "❓ Yardim", "callback_data": "yardim"}],
]


# --------------------------------------------------------------------------- #
#  Offset kaliciligi
# --------------------------------------------------------------------------- #
def _load_offset() -> int | None:
    with session_scope() as s:
        row = s.get(AppState, _OFFSET_KEY)
        if row and row.value:
            try:
                return int(row.value)
            except ValueError:
                return None
    return None


def _save_offset(value: int) -> None:
    with session_scope() as s:
        row = s.get(AppState, _OFFSET_KEY)
        if row is None:
            s.add(AppState(key=_OFFSET_KEY, value=str(value)))
        else:
            row.value = str(value)
            row.updated_at = utcnow()


# --------------------------------------------------------------------------- #
#  Komut govdeleri
# --------------------------------------------------------------------------- #
def _cmd_start() -> str:
    return (
        "🎯 <b>Alpha Hunter</b>\n\n"
        "Twitter'da paylasilan memecoin kontratlarini topluyorum ve her paylasimi "
        "<i>paylasildigi saniyedeki</i> zincir verisiyle karsilastiriyorum.\n\n"
        "Asagidaki butonlari kullanabilir ya da <b>/</b> yazip komut menusunu acabilirsin.\n\n"
        "En cok isine yarayacak olan:\n"
        "<code>/tara hesapadi 60</code> — bir hesabin son 60 gununu tarar ve puanlar."
    )


def _cmd_help() -> str:
    lines = ["❓ <b>Komutlar</b>", ""]
    for c, d in COMMANDS:
        lines.append(f"/{c} — {esc(d)}")
    lines += [
        "",
        "<b>Ornekler</b>",
        "<code>/tara elonmusk 90</code>",
        "<code>/hesap elonmusk</code>",
        "<code>/token EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm</code>",
        "<code>/top 15</code>   <code>/son 6</code>",
    ]
    return "\n".join(lines)


def _cmd_top(arg: str) -> str:
    try:
        limit = max(3, min(25, int(arg))) if arg else 10
    except ValueError:
        limit = 10
    with session_scope() as s:
        rows = latest_scores(s, limit=limit)
        if not rows:
            return (
                "Henuz skorlanmis hesap yok.\n\n"
                "Bir cagrinin kapanmasi 7 gun surer ve puan icin en az "
                f"{settings.min_calls_for_rating} tamamlanmis cagri gerekir.\n"
                "Hemen sonuc icin: <code>/tara hesapadi 60</code>"
            )
        out = [f"🏆 <b>TOP {len(rows)}</b>", ""]
        for i, r in enumerate(rows, 1):
            acc = s.get(Account, r.account_id)
            tier = r.tier.value if hasattr(r.tier, "value") else str(r.tier)
            out.append(
                f"{i}. {TIER_EMOJI.get(tier, '⚪')} <a href=\"https://x.com/{esc(acc.handle)}\">"
                f"@{esc(acc.handle)}</a> — <b>{r.alpha_score:.1f}</b>"
            )
            out.append(
                f"    isabet {r.win_rate*100:.0f}% ({r.n_wins}/{r.n_evaluated}) · "
                f"medyan {fmt_mult(r.median_multiple)} · {r.calls_per_day:.1f}/gun"
            )
        return "\n".join(out)


def _cmd_recent(arg: str) -> str:
    try:
        hours = max(1, min(168, int(arg))) if arg else 24
    except ValueError:
        hours = 24
    from ..web.queries import recent_calls

    with session_scope() as s:
        rows = recent_calls(s, hours=hours, limit=15)
    if not rows:
        return f"Son {hours} saatte cagri yok."
    out = [f"⚡ <b>Son {hours} saat — {len(rows)} cagri</b>", ""]
    for r in rows:
        tier = r["tier"]
        out.append(
            f"{TIER_EMOJI.get(tier, '⚪')} <b>{esc(r['symbol'] or r['address'][:6])}</b> · "
            f"@{esc(r['handle'])}"
        )
        out.append(
            f"    giris {fmt_usd(r['entry_mc'])} · gercekci {fmt_mult(r['sustained_multiple'])} · "
            f"<a href=\"https://dexscreener.com/{esc(r['chain'])}/{esc(r['address'])}\">grafik</a>"
        )
    return "\n".join(out)


def _cmd_account(arg: str) -> str:
    handle = jobq.normalise_handle(arg)
    if not handle:
        return "Kullanim: <code>/hesap hesapadi</code>"
    from ..web.queries import account_detail

    with session_scope() as s:
        d = account_detail(s, handle, limit=10)
    if d is None:
        return (
            f"@{esc(handle)} veritabaninda yok.\n\n"
            f"Taramak icin: <code>/tara {esc(handle)} 60</code>"
        )
    sc = d["score"]
    out = [f"👤 <b>@{esc(d['handle'])}</b>"]
    if d["blacklisted"]:
        out.append(f"⛔ kara listede: {esc(d['blacklist_reason'] or '')}")
    if d["cluster"]:
        out.append("⚠ koordineli hesap kumesinde")
    if sc:
        tier = sc["tier"]
        out += [
            f"{TIER_EMOJI.get(tier, '⚪')} alfa <b>{sc['alpha_score']}</b> ({tier})",
            f"isabet {sc['win_rate']*100:.0f}% ({sc['n_wins']}/{sc['n_evaluated']}) · "
            f"medyan {fmt_mult(sc['median_multiple'])}",
            f"giris kalitesi {sc['entry_quality']*100:.0f} · ozgunluk {sc['originality']*100:.0f} · "
            f"{sc['calls_per_day']:.1f} cagri/gun",
        ]
    else:
        out.append("Henuz skorlanmadi (yeterli tamamlanmis cagri yok).")
    if d["calls"]:
        out += ["", "<b>Son cagrilar</b>"]
        for c in d["calls"][:8]:
            out.append(
                f"· {esc(c['symbol'] or c['address'][:6])} — giris {fmt_usd(c['entry_mc'])} → "
                f"{fmt_mult(c['sustained_multiple'])} [{c['outcome']}]"
            )
    return "\n".join(out)


def _cmd_jobs() -> str:
    with session_scope() as s:
        rows = jobq.list_jobs(s, limit=10)
    if not rows:
        return "Henuz tarama isi yok.\n\nBaslatmak icin: <code>/tara hesapadi 60</code>"
    icon = {"queued": "⏳", "running": "⚙️", "done": "✅", "failed": "❌"}
    out = ["🔎 <b>Tarama isleri</b>", ""]
    for j in rows:
        out.append(
            f"{icon.get(j['status'], '·')} #{j['id']} @{esc(j['target'])} ({j['days']} gun) — "
            f"{esc(j['progress'] or j['status'])}"
        )
        r = j.get("result") or {}
        if j["status"] == "done" and r.get("found"):
            out.append(
                f"    alfa <b>{r['alpha_score']}</b> ({r['tier']}) · "
                f"isabet {r['win_rate']*100:.0f}% ({r['n_wins']}/{r['n_evaluated']})"
            )
        elif j["status"] == "done":
            out.append("    kontrat adresi iceren tweet bulunamadi")
        elif j["status"] == "failed":
            out.append(f"    {esc((j.get('error') or '')[:90])}")
    return "\n".join(out)


def _cmd_scan(arg: str, chat_id: str) -> str:
    parts = (arg or "").split()
    if not parts:
        return (
            "Kullanim: <code>/tara hesapadi 60</code>\n\n"
            "Ikinci sayi kac gun geriye bakilacagini soyler (varsayilan 60, en fazla 365)."
        )
    handle = jobq.normalise_handle(parts[0])
    if not handle:
        return f"'{esc(parts[0])}' gecerli bir hesap adi degil."
    days = jobq.clamp_days(parts[1] if len(parts) > 1 else None)
    job, msg = jobq.enqueue_backfill(handle, days, source="telegram", notify_chat_id=chat_id)
    if job is None:
        return f"❌ {esc(msg)}"
    return (
        f"⏳ <b>{esc(msg)}</b>\n\n"
        "Ucretsiz fiyat API'si yavas, birkac dakika surebilir. "
        "Bitince buraya haber verecegim.\n"
        "Durumu gormek icin: /isler"
    )


def _cmd_status() -> str:
    from sqlalchemy import func

    from ..db.session import db_status

    with session_scope() as s:
        n_acc = s.scalar(select(func.count()).select_from(Account)) or 0
        n_call = s.scalar(select(func.count()).select_from(Call)) or 0
        n_tok = s.scalar(select(func.count()).select_from(Token)) or 0
        last = s.scalar(select(func.max(Call.called_at)))
        n_24 = s.scalar(
            select(func.count(Call.id)).where(Call.called_at >= utcnow() - __import__(
                "datetime").timedelta(hours=24))
        ) or 0
        n_queued = s.scalar(
            select(func.count(Job.id)).where(Job.status.in_(["queued", "running"]))
        ) or 0
    st = db_status()
    return "\n".join([
        "📊 <b>Sistem durumu</b>",
        "",
        f"izlenen hesap: <b>{n_acc}</b>",
        f"cagri: <b>{n_call}</b> (son 24 saat: {n_24})",
        f"token: <b>{n_tok}</b>",
        f"son cagri: {last.strftime('%d.%m %H:%M') if last else '—'}",
        f"kuyruktaki tarama: {n_queued}",
        "",
        f"zincir: {', '.join(settings.chain_list)}",
        f"kaynak: {', '.join(settings.source_list)}",
        f"veritabani: {'bagli' if st['ok'] else 'BAGLI DEGIL'}",
    ])


async def _cmd_token(arg: str, http: HttpClient) -> str:
    addr = (arg or "").strip().split()[0] if arg else ""
    if not addr:
        return "Kullanim: <code>/token &lt;kontrat adresi&gt;</code>"
    from ..oracle.resolver import PriceOracle

    oracle = PriceOracle(http)
    info = None
    for chain in settings.chain_list:
        info = await oracle.token_info(chain, addr)
        if info:
            break
    if info is None:
        return "Bu adres hicbir zincirde bulunamadi."
    out = [
        f"🪙 <b>{esc(info.symbol or '?')}</b> — {esc(info.name or '')}",
        f"<code>{esc(info.address)}</code>",
        "",
        f"MC {fmt_usd(info.mc_usd)} · likidite {fmt_usd(info.liquidity_usd)}",
        f"24s hacim {fmt_usd(info.volume_24h)} · havuz {esc(info.dex_id or '?')}",
    ]
    with session_scope() as s:
        tok = s.scalar(select(Token).where(Token.address == info.address))
        if tok:
            calls = list(
                s.scalars(
                    select(Call).where(Call.token_id == tok.id).order_by(Call.called_at.asc())
                )
            )
            if calls:
                out += ["", f"<b>Bu CA'yi {len(calls)} kez paylasan hesaplar</b>"]
                for c in calls[:8]:
                    a = s.get(Account, c.account_id)
                    out.append(
                        f"#{c.caller_rank or '?'} @{esc(a.handle if a else '?')} — "
                        f"giris {fmt_usd(c.entry_mc_usd)}"
                    )
    return "\n".join(out)


# --------------------------------------------------------------------------- #
#  Yonlendirici
# --------------------------------------------------------------------------- #
async def handle_command(cmd: str, arg: str, chat_id: str, http: HttpClient) -> tuple[str, bool]:
    """(cevap metni, ana klavyeyi ekle mi) doner."""
    cmd = cmd.lower().lstrip("/").split("@")[0]
    if cmd in ("start", "basla"):
        return (_cmd_start(), True)
    if cmd in ("yardim", "help", "komutlar"):
        return (_cmd_help(), True)
    if cmd in ("top", "liderlik"):
        return (_cmd_top(arg), True)
    if cmd in ("son", "recent", "cagrilar"):
        return (_cmd_recent(arg), True)
    if cmd in ("tara", "scan", "backfill"):
        return (_cmd_scan(arg, chat_id), False)
    if cmd in ("hesap", "account"):
        return (_cmd_account(arg), False)
    if cmd in ("isler", "jobs"):
        return (_cmd_jobs(), True)
    if cmd in ("durum", "status"):
        return (_cmd_status(), True)
    if cmd == "token":
        return (await _cmd_token(arg, http), False)
    return (
        f"Bilinmeyen komut: /{esc(cmd)}\n\nKomutlar icin /yardim ya da <b>/</b> yaz.",
        True,
    )


def _authorised(chat_id: str) -> bool:
    """Yalnizca yapilandirilmis sohbet komut verebilir."""
    allowed = (settings.telegram_chat_id or "").strip()
    return bool(allowed) and str(chat_id) == allowed


async def process_updates(notifier: TelegramNotifier, http: HttpClient) -> int:
    """Bir tur yoklama yapip gelen komutlari isler. Islenen mesaj sayisini doner."""
    offset = _load_offset()
    updates = await notifier.get_updates(offset, http)
    handled = 0

    for up in updates:
        _save_offset(up["update_id"] + 1)

        msg = up.get("message") or {}
        cb = up.get("callback_query") or {}

        if cb:
            chat_id = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
            await notifier.answer_callback(cb.get("id", ""), http)
            if not _authorised(chat_id):
                continue
            text, kb = await handle_command(cb.get("data", ""), "", chat_id, http)
            await notifier.send_to(chat_id, text, http, MAIN_KEYBOARD if kb else None)
            handled += 1
            continue

        chat_id = str((msg.get("chat") or {}).get("id", ""))
        body = (msg.get("text") or "").strip()
        if not chat_id or not body:
            continue

        if not _authorised(chat_id):
            log.warning("yetkisiz sohbetten komut: %s", chat_id)
            await notifier.send_to(
                chat_id,
                "Bu bot ozeldir ve yalnizca sahibinin sohbetinde calisir.",
                http,
            )
            continue

        if not body.startswith("/"):
            # Duz metin: hesap adi gibi duruyorsa tarama teklif et
            guess = jobq.normalise_handle(body)
            hint = (
                f"\n\nBunu taramak icin: <code>/tara {esc(guess)} 60</code>" if guess else ""
            )
            await notifier.send_to(
                chat_id, "Komutlar icin <b>/</b> yaz ya da /yardim." + hint, http, MAIN_KEYBOARD
            )
            handled += 1
            continue

        parts = body.split(maxsplit=1)
        cmd = parts[0]
        arg = parts[1] if len(parts) > 1 else ""
        try:
            text, kb = await handle_command(cmd, arg, chat_id, http)
        except Exception as exc:
            log.exception("komut hatasi: %s", body[:60])
            text, kb = (f"Komut calistirilamadi: {esc(type(exc).__name__)}", True)
        await notifier.send_to(chat_id, text, http, MAIN_KEYBOARD if kb else None)
        handled += 1

    return handled


async def announce_job_result(job_id: int) -> None:
    """Telegram'dan baslatilan tarama bitince sonucu geri bildirir."""
    notifier = TelegramNotifier()
    if not notifier.token:
        return
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None or not job.notify_chat_id:
            return
        chat_id = job.notify_chat_id
        d = jobq.job_to_dict(job)

    r = d.get("result") or {}
    if d["status"] == "failed":
        text = f"❌ <b>@{esc(d['target'])} taramasi basarisiz</b>\n{esc((d.get('error') or '')[:200])}"
    elif not r.get("found"):
        text = (
            f"🔍 <b>@{esc(d['target'])}</b> — son {d['days']} gunde kontrat adresi iceren "
            "tweet bulunamadi.\n\n"
            "Hesap gercekten CA paylasmiyor olabilir, ya da tweet kaynagi veri dondurmedi."
        )
    else:
        tier = r["tier"]
        text = "\n".join([
            f"✅ <b>@{esc(r['handle'])} tarandi</b> ({d['days']} gun)",
            "",
            f"{TIER_EMOJI.get(tier, '⚪')} alfa <b>{r['alpha_score']}</b> ({tier})",
            f"isabet {r['win_rate']*100:.0f}% ({r['n_wins']}/{r['n_evaluated']}) · "
            f"medyan {fmt_mult(r['median_multiple'])}",
            f"giris kalitesi {r['entry_quality']*100:.0f} · "
            f"ozgunluk {r['originality']*100:.0f} · {r['calls_per_day']:.1f} cagri/gun",
            "",
            f"Detay: <code>/hesap {esc(r['handle'])}</code>",
        ])
    async with HttpClient() as http:
        await notifier.send_to(chat_id, text, http, MAIN_KEYBOARD)
