"""Komut yuzeyi — bilerek KUCUK.

Referans urunde 18 komut var; cogu ayni veriyi baska bir baslikla
gosteriyor ya da olculemeyen bir yorum uretiyor. Burada 7 komut var ve
her biri farkli bir soruya cevap veriyor:

    /radar   — su an ne oluyor?
    /liste   — benim tokenlerim ne durumda?
    /cuzdan  — kime bakiyorsun ve nicin?
    /karne   — bana verdigin sinyaller para kazandirdi mi?
    /ayar    — esikleri degistir
    /durum   — sistem calisiyor mu?
    /yardim  — komutlar

Bunlarin disinda komut YAZMANA GEREK YOK: kontrat adresini yapistir,
rapor gelsin.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from sqlalchemy import func, select

from ..config import settings
from ..core import journal, prefs, repo
from ..core.wallets import smart_wallets
from ..db import (
    Alert,
    AlertKind,
    Outcome,
    Token,
    Trade,
    Wallet,
    Watch,
    db_healthy,
    session_scope,
    utcnow,
)
from ..http import HttpClient
from ..sources.dexscreener import DexScreener
from ..sources.safety import check_token
from . import cards
from .telegram import esc, fmt_mult, fmt_pct, fmt_usd

log = logging.getLogger(__name__)

COMMANDS: list[tuple[str, str]] = [
    ("radar", "Son 24 saatteki sinyaller"),
    ("liste", "Takip listem"),
    ("cuzdan", "Sistemin buldugu akilli cuzdanlar"),
    ("karne", "Botun gercek sicili"),
    ("ayar", "Esikler"),
    ("durum", "Sistem durumu"),
    ("yardim", "Komutlar"),
]

MAIN_KEYBOARD = [
    [{"text": "📡 Radar", "callback_data": "radar"},
     {"text": "📋 Listem", "callback_data": "liste"}],
    [{"text": "👛 Cuzdanlar", "callback_data": "cuzdan"},
     {"text": "🧾 Karne", "callback_data": "karne"}],
    [{"text": "⚙️ Ayar", "callback_data": "ayar"},
     {"text": "📊 Durum", "callback_data": "durum"}],
]

# Solana base58 mint ya da EVM adresi
RE_SOLANA = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
RE_EVM = re.compile(r"\b0x[a-fA-F0-9]{40}\b")


def extract_address(text: str) -> str | None:
    m = RE_EVM.search(text)
    if m:
        return m.group(0)
    for cand in RE_SOLANA.findall(text):
        # URL parcalarini ve yaygin kelimeleri ele
        if cand.lower() in ("dexscreener", "geckoterminal"):
            continue
        return cand
    return None


# --------------------------------------------------------------------------- #
#  /start · /yardim
# --------------------------------------------------------------------------- #
def cmd_start() -> str:
    return "\n".join([
        "📡 <b>Radar</b> — akilli para konfluans botu",
        "",
        "Grafik sana olan biteni gosterir. Ben <b>kimin aldigini</b> gosteriyorum.",
        "",
        "Tek cuzdanin alimi gurultudur. Birbirinden bagimsiz birden cok cuzdan "
        "ayni tokende ayni yone dondugunde tesadüf ihtimali hizla duser. "
        "Sana yalnizca bunu bildiriyorum.",
        "",
        "<b>Kadro elle secilmez.</b> Her cuzdan kendi alimlarindan olculur: "
        "alimdan sonraki korunmus tepe, Wilson alt siniri, medyan kat, giris "
        "piyasa degeri. Sicili bozulan kadrodan duser.",
        "",
        "<b>Kendi karnemi tutuyorum.</b> Gonderdigim her kart +1s/+6s/+24s'te "
        "olculur. /karne yaz, gercek sayilari gor — kotuyse de gorursun.",
        "",
        "🚀 <b>Baslamak icin:</b> bir kontrat adresini buraya yapistir.",
    ])


def cmd_help() -> str:
    lines = ["❓ <b>Komutlar</b>", ""]
    lines += [f"/{c} — {esc(d)}" for c, d in COMMANDS]
    lines += [
        "",
        "<b>Komut gerektirmeyenler</b>",
        "· Kontrat adresi yapistir → tam token raporu",
        "",
        "<b>Ornekler</b>",
        "<code>/ayar esik_cuzdan 4</code>",
        "<code>/ayar esik_likidite 25000</code>",
        "<code>/karne 7</code>  (son 7 gun)",
        "<code>/radar 6</code>  (son 6 saat)",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  /radar
# --------------------------------------------------------------------------- #
def cmd_radar(arg: str) -> str:
    try:
        hours = max(1, min(168, int(arg))) if arg.strip() else 24
    except ValueError:
        hours = 24
    since = utcnow() - timedelta(hours=hours)

    with session_scope() as s:
        rows = list(
            s.scalars(
                select(Alert)
                .where(Alert.created_at >= since)
                .order_by(Alert.created_at.desc())
                .limit(20)
            )
        )
        if not rows:
            n_smart = s.scalar(select(func.count(Wallet.id)).where(Wallet.smart.is_(True))) or 0
            n_tok = s.scalar(select(func.count(Token.id)).where(Token.active.is_(True))) or 0
            return "\n".join([
                f"📡 Son {hours} saatte sinyal yok.",
                "",
                f"izlenen token: <b>{n_tok}</b> · akilli cuzdan: <b>{n_smart}</b>",
                "",
                "<i>Sinyal uretmemek bir hata degil, ozelliktir. Konfluans "
                f"icin {prefs.get('esik_cuzdan')} bagimsiz cuzdan gerekiyor; "
                "kosul olusmadan kart gondermem.</i>"
                + ("\n\nHenuz akilli cuzdan kadrosu olusmadi — sistemin sicil "
                   "biriktirmesi icin birkac gune ihtiyaci var." if n_smart == 0 else ""),
            ])

        out = [f"📡 <b>Son {hours} saat — {len(rows)} sinyal</b>", ""]
        for al in rows:
            tok = s.get(Token, al.token_id)
            if tok is None:
                continue
            p = al.data()
            sym = esc(tok.symbol or tok.address[:6])
            when = al.created_at.strftime("%d.%m %H:%M")
            head = cards.KIND_TITLE.get(al.kind, "🔔").split()[0]
            extra = ""
            if al.kind == AlertKind.CONFLUENCE.value:
                extra = f"{p.get('cuzdan_sayisi', '?')} cuzdan"
            elif al.kind == AlertKind.VOLUME.value:
                extra = f"{p.get('kat', '?')}x hacim"
            elif al.kind == AlertKind.WATCH_PRICE.value:
                extra = f"%{p.get('yuzde', 0)}"
            now_mult = None
            if al.entry_mc_usd and tok.last_mc_usd:
                now_mult = tok.last_mc_usd / al.entry_mc_usd
            out.append(
                f"{head} <b>{sym}</b> · {esc(extra)} · {when}"
            )
            out.append(
                f"    giris {fmt_usd(al.entry_mc_usd)} → simdi {fmt_mult(now_mult)}"
                + (f" · tepe {fmt_mult(al.peak_mult)}" if al.peak_mult else "")
            )
        out += ["", "<i>Detay icin kontrat adresini yapistir.</i>"]
        return "\n".join(out)


# --------------------------------------------------------------------------- #
#  /liste
# --------------------------------------------------------------------------- #
def cmd_list(chat_id: str) -> tuple[str, list[list[dict]]]:
    with session_scope() as s:
        rows = list(
            s.scalars(
                select(Watch).where(Watch.chat_id == chat_id).order_by(Watch.added_at.desc())
            )
        )
        if not rows:
            return (
                "📋 Takip listen bos.\n\n"
                "Bir kontrat adresi yapistir, altindaki <b>➕ Takibe al</b> "
                "butonuna bas.",
                MAIN_KEYBOARD,
            )
        out = [f"📋 <b>Takip listen — {len(rows)} token</b>", ""]
        keyboard: list[list[dict]] = []
        for w in rows[:25]:
            tok = s.get(Token, w.token_id)
            if tok is None:
                continue
            chg = None
            if w.ref_price_usd and tok.last_price_usd:
                chg = (tok.last_price_usd / w.ref_price_usd - 1) * 100
            sym = esc(tok.symbol or tok.address[:6])
            mute = " 🔕" if w.muted else ""
            out.append(
                f"· <b>{sym}</b>{mute} — MC {fmt_usd(tok.last_mc_usd)} · "
                f"likidite {fmt_usd(tok.last_liquidity_usd)}"
                + (f" · ref'e gore %{chg:+.1f}" if chg is not None else "")
            )
            esik = w.price_pct or prefs.get("esik_fiyat")
            out.append(f"    esik %{esik:g} · guvenlik {tok.safety_score if tok.safety_score is not None else '—'}")
            keyboard.append([
                {"text": f"📊 {tok.symbol or tok.address[:5]}", "callback_data": f"t:{tok.id}"},
                {"text": "🔔" if w.muted else "🔕", "callback_data": f"wm:{tok.id}"},
                {"text": "🗑", "callback_data": f"wd:{tok.id}"},
            ])
        return "\n".join(out), keyboard[:12]


# --------------------------------------------------------------------------- #
#  /cuzdan
# --------------------------------------------------------------------------- #
def cmd_wallets(arg: str) -> str:
    try:
        limit = max(3, min(25, int(arg))) if arg.strip() else 12
    except ValueError:
        limit = 12

    with session_scope() as s:
        rows = smart_wallets(s, limit=limit)
        n_all = s.scalar(select(func.count(Wallet.id))) or 0
        n_blocked = s.scalar(select(func.count(Wallet.id)).where(Wallet.blocked.is_(True))) or 0
        n_graded = s.scalar(
            select(func.count(Wallet.id)).where(
                Wallet.n_evaluated >= settings.wallet_min_evaluated
            )
        ) or 0

    if not rows:
        return "\n".join([
            "👛 Henuz akilli cuzdan kadrosu yok.",
            "",
            f"gorulen cuzdan: <b>{n_all}</b>",
            f"yeterli sicili olan: <b>{n_graded}</b> "
            f"(en az {settings.wallet_min_evaluated} sonuclanmis alim)",
            f"elenen (sprey/dust/altyapi): <b>{n_blocked}</b>",
            "",
            "<i>Bir alimin sonuclanmasi "
            f"{settings.wallet_eval_hours} saat surer. Kadro kendiliginden "
            "olusur; elle cuzdan eklemiyorum cunku olculmemis cuzdan "
            "bilgi degil, tahmindir.</i>",
        ])

    out = [
        f"👛 <b>AKILLI CUZDANLAR — {len(rows)}</b>",
        f"<i>{n_all} cuzdan goruldu, {n_blocked} tanesi sprey/dust/altyapi "
        f"olarak elendi.</i>",
        "",
    ]
    for i, w in enumerate(rows, 1):
        out.append(
            f"{i}. <code>{esc(w['kisa'])}</code> — skor <b>{w['skor']:.0f}</b>"
        )
        out.append(
            f"    isabet {fmt_pct(w['isabet'])} ({w['kazanan']}/{w['n']}) · "
            f"wilson {w['wilson']:.2f} · medyan {fmt_mult(w['medyan_kat'])}"
        )
        out.append(
            f"    medyan giris {fmt_usd(w['medyan_giris_mc'])} · "
            f"{w['token']} farkli token"
        )
    out += [
        "",
        "<i>Wilson alt siniri ham isabet oraninin yerine gecer: 3 atisin "
        "3'unu tutturan cuzdan %100 degil, 0.31'dir.</i>",
    ]
    return "\n".join(out)


# --------------------------------------------------------------------------- #
#  /karne
# --------------------------------------------------------------------------- #
def cmd_scorecard(arg: str) -> str:
    try:
        days = max(1, min(180, int(arg))) if arg.strip() else 30
    except ValueError:
        days = 30

    with session_scope() as s:
        card = journal.scorecard(s, days=days)

    g = card["genel"]
    if not g["toplam"]:
        return (
            f"🧾 Son {days} gunde hic kart gondermedim.\n\n"
            "<i>Bos karne, kotu karneden iyidir: kosul olusmadan kart uretmiyorum.</i>"
        )

    out = [
        f"🧾 <b>KARNE — son {days} gun</b>",
        "",
        f"toplam kart: <b>{g['toplam']}</b> (acik {g['acik']}, kapali {g['kapali']})",
    ]
    if g["kapali"]:
        out += [
            f"isabet: <b>{g['isabet']}</b> · zarar: <b>{g['zarar']}</b> · "
            f"oran <b>{fmt_pct(g['isabet_orani'])}</b>",
            f"medyan tepe {fmt_mult(g['medyan_tepe'])} · "
            f"medyan 24s {fmt_mult(g['medyan_24s'])}",
        ]
    out.append("")
    out.append("<b>Sinyal tipine gore</b>")
    for kind, st in card["tip"].items():
        label = cards.KIND_LABEL.get(kind, kind)
        if not st["kapali"]:
            out.append(f"· {esc(label)} — {st['toplam']} kart, hepsi acik")
            continue
        out.append(
            f"· <b>{esc(label)}</b> — {st['kapali']} kart · "
            f"isabet {fmt_pct(st['isabet_orani'])} ({st['isabet']}/{st['kapali']})"
        )
        out.append(
            f"    medyan tepe {fmt_mult(st['medyan_tepe'])} · "
            f"medyan 24s {fmt_mult(st['medyan_24s'])} · zarar {st['zarar']}"
        )
    out += [
        "",
        f"<i>ISABET = kart geldikten sonra korunmus tepe girisin "
        f"{settings.journal_win_multiple:g} katini gecti. "
        f"ZARAR = 24 saat sonunda girisin %"
        f"{settings.journal_loss_multiple * 100:.0f}'inin altinda.</i>",
        "",
        "<i>Bir sinyal tipinin orani surekli dusukse o tipi kapat: "
        "kotu sayiyi saklamiyorum, kararini bu tabloya bakarak ver.</i>",
    ]
    return "\n".join(out)


# --------------------------------------------------------------------------- #
#  /ayar
# --------------------------------------------------------------------------- #
def cmd_settings(arg: str) -> str:
    parts = arg.split()
    if len(parts) >= 2:
        ok, msg = prefs.set_value(parts[0].lower(), parts[1])
        prefix = "✅" if ok else "❌"
        return f"{prefix} {esc(msg)}\n\n" + cmd_settings("")

    out = ["⚙️ <b>Ayarlar</b>", ""]
    for key, val, desc in prefs.all_values():
        shown = f"{val:g}" if isinstance(val, (int, float)) else str(val)
        out.append(f"<code>{esc(key)}</code> = <b>{esc(shown)}</b>")
        out.append(f"    {esc(desc)}")
    out += [
        "",
        "Degistirmek icin: <code>/ayar esik_cuzdan 4</code>",
        "",
        "<i>Yalnizca uc esik ayarlanabilir. Her sayiyi ayarlanabilir yapmak "
        "kullaniciyi ayar menusunde bogar; onemli olan uc tanesi bunlar.</i>",
    ]
    return "\n".join(out)


# --------------------------------------------------------------------------- #
#  /durum
# --------------------------------------------------------------------------- #
def cmd_status() -> str:
    from ..db import PriceSnapshot

    with session_scope() as s:
        n_tok = s.scalar(select(func.count(Token.id))) or 0
        n_active = s.scalar(select(func.count(Token.id)).where(Token.active.is_(True))) or 0
        n_trade = s.scalar(select(func.count(Trade.id))) or 0
        n_wallet = s.scalar(select(func.count(Wallet.id))) or 0
        n_smart = s.scalar(select(func.count(Wallet.id)).where(Wallet.smart.is_(True))) or 0
        n_alert = s.scalar(select(func.count(Alert.id))) or 0
        n_open = s.scalar(
            select(func.count(Alert.id)).where(Alert.outcome == Outcome.OPEN.value)
        ) or 0
        n_snap = s.scalar(select(func.count(PriceSnapshot.id))) or 0
        last_trade = s.scalar(select(func.max(Trade.at)))
        last_snap = s.scalar(select(func.max(PriceSnapshot.at)))

    def freshness(ts) -> str:
        if ts is None:
            return "🔴 hic"
        mins = (utcnow() - ts).total_seconds() / 60
        icon = "🟢" if mins < 30 else ("🟡" if mins < 120 else "🔴")
        return f"{icon} {cards.age_str(ts)} once"

    return "\n".join([
        "📊 <b>Sistem durumu</b>",
        "",
        f"veritabani: {'🟢 bagli' if db_healthy() else '🔴 BAGLI DEGIL'}",
        f"son islem verisi: {freshness(last_trade)}",
        f"son fiyat ornegi: {freshness(last_snap)}",
        "",
        f"token: <b>{n_tok}</b> (aktif izlenen {n_active})",
        f"fiyat ornegi: <b>{n_snap:,}</b>",
        f"islem: <b>{n_trade:,}</b>",
        f"cuzdan: <b>{n_wallet:,}</b> (akilli {n_smart})",
        f"alarm: <b>{n_alert}</b> (olculmeyi bekleyen {n_open})",
        "",
        f"zincir: {esc(', '.join(settings.chains))}",
        "<i>Kaynaklar: GeckoTerminal + DexScreener + RugCheck + GoPlus — "
        "hepsi bedava katman, anahtar gerektirmez.</i>",
    ])


# --------------------------------------------------------------------------- #
#  Kontrat adresi raporu
# --------------------------------------------------------------------------- #
async def token_report(address: str, chat_id: str, http: HttpClient) -> tuple[str, list[list[dict]]]:
    ds = DexScreener(http)
    snap = None
    for chain in settings.chains:
        snap = await ds.token(address, chain=chain)
        if snap:
            break
    if snap is None:
        # Zincir filtresi tutmadiysa filtresiz dene
        snap = await ds.token(address)
    if snap is None:
        return (
            f"❌ Bu adres icin islem goren bir havuz bulamadim.\n\n"
            f"<code>{esc(address)}</code>\n\n"
            "<i>Token yeni dogmus ya da likiditesi cekilmis olabilir.</i>",
            MAIN_KEYBOARD,
        )

    with session_scope() as s:
        tok, _ = repo.upsert_token(
            s, snap.chain, snap.address,
            symbol=snap.symbol, name=snap.name, pair_address=snap.pair_address,
            dex_id=snap.dex_id, price_usd=snap.price_usd, mc_usd=snap.mc_usd,
            liquidity_usd=snap.liquidity_usd, volume_h1=snap.volume_h1,
            volume_h24=snap.volume_h24, pool_created_at=snap.pair_created_at,
        )
        token_id = tok.id

    born = snap.pair_created_at
    age_min = (utcnow() - born).total_seconds() / 60 if born else None
    rep = await check_token(
        http, snap.chain, snap.address,
        liquidity_usd=snap.liquidity_usd, mc_usd=snap.mc_usd, age_minutes=age_min,
        buy_pressure=snap.buy_pressure, volume_h24=snap.volume_h24,
    )

    with session_scope() as s:
        tok = s.get(Token, token_id)
        repo.set_safety(s, tok, rep.score, rep.flags)

        watched = bool(
            s.scalar(
                select(Watch.id).where(Watch.chat_id == chat_id, Watch.token_id == token_id)
            )
        )

        # Akilli para bu tokene dokundu mu?
        smart_ids = repo.smart_wallet_ids(s)
        buys = sells = 0
        buy_usd = sell_usd = 0.0
        if smart_ids:
            since = utcnow() - timedelta(days=7)
            for side, n, usd in s.execute(
                select(Trade.side, func.count(func.distinct(Trade.wallet_id)), func.sum(Trade.usd))
                .where(
                    Trade.token_id == token_id,
                    Trade.at >= since,
                    Trade.wallet_id.in_(smart_ids),
                )
                .group_by(Trade.side)
            ):
                if side == "buy":
                    buys, buy_usd = int(n), float(usd or 0)
                else:
                    sells, sell_usd = int(n), float(usd or 0)

        # Gecmiste bu token icin kart gonderdik mi? ("su tarihte demistik")
        past = list(
            s.scalars(
                select(Alert)
                .where(Alert.token_id == token_id)
                .order_by(Alert.created_at.desc())
                .limit(3)
            )
        )
        past_lines = []
        for al in past:
            mult = None
            if al.entry_mc_usd and tok.last_mc_usd:
                mult = tok.last_mc_usd / al.entry_mc_usd
            past_lines.append(
                f"· {al.created_at.strftime('%d.%m %H:%M')} — "
                f"{esc(cards.KIND_LABEL.get(al.kind, al.kind))} @ {fmt_usd(al.entry_mc_usd)} "
                f"→ bugun {fmt_mult(mult)}"
            )

        lines = [
            f"🪙 <b>{esc(snap.symbol or '?')}</b> — {esc(snap.name or '')}",
            f"<code>{esc(snap.chain)}</code> · {esc(snap.dex_id or '?')}",
            "",
            *cards.market_lines(tok),
        ]
        if snap.price_change_h1 is not None or snap.price_change_h24 is not None:
            lines.append(
                f"degisim 1s %{snap.price_change_h1 or 0:+.1f} · "
                f"24s %{snap.price_change_h24 or 0:+.1f}"
            )
        bp = snap.buy_pressure
        if bp is not None:
            lines.append(
                f"1s alim/satim dengesi: %{bp * 100:.0f} alim "
                f"({snap.buys_h1}/{(snap.buys_h1 or 0) + (snap.sells_h1 or 0)})"
            )

        lines += ["", f"<b>Guvenlik</b> {cards.safety_line(tok)}"]
        if rep.flags:
            for f in rep.flags[:5]:
                lines.append(f"  · {esc(f)}")
        if rep.fatal:
            lines.append("  🔴 <b>OLUMCUL BAYRAK — bu token icin kart uretmem.</b>")

        lines += ["", "<b>Akilli para (7 gun)</b>"]
        if not smart_ids:
            lines.append("  kadro henuz olusmadi — /cuzdan")
        elif buys or sells:
            yon = "birikim" if buys > sells else ("dagitim" if sells > buys else "karisik")
            lines.append(
                f"  {esc(yon)} — alan {buys} cuzdan ({fmt_usd(buy_usd)}) · "
                f"satan {sells} cuzdan ({fmt_usd(sell_usd)})"
            )
        else:
            lines.append("  kadrodan hicbir cuzdan bu tokene dokunmadi")

        if past_lines:
            lines += ["", "<b>Gecmiste ne demistim</b>", *past_lines]

        lines += cards.ca_block(tok)
        lines += ["", "<i>Yatirim tavsiyesi degildir.</i>"]

        keyboard = cards.alert_keyboard(tok, watched)
        return "\n".join(lines), keyboard


# --------------------------------------------------------------------------- #
#  Takip listesi islemleri
# --------------------------------------------------------------------------- #
def watch_add(chat_id: str, token_id: int) -> str:
    with session_scope() as s:
        tok = s.get(Token, token_id)
        if tok is None:
            return "Token bulunamadi."
        n = s.scalar(select(func.count(Watch.id)).where(Watch.chat_id == chat_id)) or 0
        if n >= settings.watch_max_tokens:
            return f"Takip listesi dolu ({settings.watch_max_tokens}). Once birini cikar."
        existing = s.scalar(
            select(Watch).where(Watch.chat_id == chat_id, Watch.token_id == token_id)
        )
        if existing:
            return f"<b>{esc(tok.symbol or tok.address[:6])}</b> zaten listende."
        s.add(
            Watch(
                chat_id=chat_id,
                token_id=token_id,
                ref_price_usd=tok.last_price_usd,
                ref_at=utcnow(),
            )
        )
        tok.active = True
        return (
            f"➕ <b>{esc(tok.symbol or tok.address[:6])}</b> takibe alindi.\n"
            f"Esik %{prefs.get('esik_fiyat'):g} — degistirmek icin /ayar"
        )


def watch_remove(chat_id: str, token_id: int) -> str:
    with session_scope() as s:
        w = s.scalar(select(Watch).where(Watch.chat_id == chat_id, Watch.token_id == token_id))
        if w is None:
            return "Bu token listende degil."
        tok = s.get(Token, token_id)
        s.delete(w)
        return f"🗑 <b>{esc(tok.symbol if tok else token_id)}</b> listeden cikarildi."


def watch_mute(chat_id: str, token_id: int) -> str:
    with session_scope() as s:
        w = s.scalar(select(Watch).where(Watch.chat_id == chat_id, Watch.token_id == token_id))
        if w is None:
            return "Bu token listende degil."
        w.muted = not w.muted
        tok = s.get(Token, token_id)
        sym = esc(tok.symbol if tok else token_id)
        return f"{'🔕' if w.muted else '🔔'} <b>{sym}</b> {'susturuldu' if w.muted else 'acildi'}."


async def token_report_by_id(token_id: int, chat_id: str, http: HttpClient):
    with session_scope() as s:
        tok = s.get(Token, token_id)
        addr = tok.address if tok else None
    if not addr:
        return ("Token bulunamadi.", MAIN_KEYBOARD)
    return await token_report(addr, chat_id, http)


# --------------------------------------------------------------------------- #
#  Yonlendirici
# --------------------------------------------------------------------------- #
async def handle(text: str, chat_id: str, http: HttpClient) -> tuple[str, list[list[dict]] | None]:
    body = (text or "").strip()
    if not body:
        return ("Komutlar icin /yardim.", MAIN_KEYBOARD)

    # Buton geri cagrilari
    if ":" in body and body.split(":", 1)[0] in ("t", "wa", "wd", "wm"):
        action, _, raw = body.partition(":")
        try:
            token_id = int(raw)
        except ValueError:
            return ("Gecersiz buton.", MAIN_KEYBOARD)
        if action == "wa":
            return (watch_add(chat_id, token_id), MAIN_KEYBOARD)
        if action == "wd":
            return (watch_remove(chat_id, token_id), MAIN_KEYBOARD)
        if action == "wm":
            return (watch_mute(chat_id, token_id), MAIN_KEYBOARD)
        return await token_report_by_id(token_id, chat_id, http)

    if not body.startswith("/"):
        addr = extract_address(body)
        if addr:
            return await token_report(addr, chat_id, http)
        return (
            "Kontrat adresi yapistir, rapor gelsin.\n\nKomutlar icin /yardim.",
            MAIN_KEYBOARD,
        )

    parts = body.split(maxsplit=1)
    cmd = parts[0].lstrip("/").split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in ("start", "basla"):
        return (cmd_start(), MAIN_KEYBOARD)
    if cmd in ("yardim", "help", "komutlar"):
        return (cmd_help(), MAIN_KEYBOARD)
    if cmd in ("radar", "sinyal", "sinyaller"):
        return (cmd_radar(arg), MAIN_KEYBOARD)
    if cmd in ("liste", "list", "takip"):
        return cmd_list(chat_id)
    if cmd in ("cuzdan", "cuzdanlar", "wallets"):
        return (cmd_wallets(arg), MAIN_KEYBOARD)
    if cmd in ("karne", "sicil", "score"):
        return (cmd_scorecard(arg), MAIN_KEYBOARD)
    if cmd in ("ayar", "ayarlar", "settings"):
        return (cmd_settings(arg), MAIN_KEYBOARD)
    if cmd in ("durum", "status"):
        return (cmd_status(), MAIN_KEYBOARD)

    addr = extract_address(body)
    if addr:
        return await token_report(addr, chat_id, http)
    return (f"Bilinmeyen komut: /{esc(cmd)}\n\n/yardim yaz.", MAIN_KEYBOARD)
