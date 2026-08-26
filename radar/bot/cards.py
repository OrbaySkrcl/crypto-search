"""Kart bicimleme.

Kural: her kart YALNIZCA olculmus sayi tasir. Yorum yok, tahmin yok,
"bu coin ucabilir" yok. Kullanicinin karar verebilmesi icin gereken
dort sey verilir:

    ne oldu · ne kadar buyuk · ne kadar riskli · bu tip sinyal gecmiste ne yapti

Son satir en onemlisidir: kart kendi gecmis basarisini uzerinde tasir.
Bir sinyal tipi ise yaramiyorsa kullanici bunu kartin uzerinde gorur.
"""
from __future__ import annotations

from datetime import datetime

from ..config import EXPLORER
from ..core import journal
from ..core.stats import tradeable_usd
from ..db import Alert, AlertKind, Token, utcnow
from .telegram import esc, fmt_mult, fmt_pct, fmt_usd

KIND_TITLE = {
    AlertKind.CONFLUENCE.value: "🎯 KONFLUANS",
    AlertKind.VOLUME.value: "📈 SIRA DISI HACIM",
    AlertKind.WATCH_PRICE.value: "🔔 TAKIP — FIYAT",
    AlertKind.WATCH_SMART.value: "🔔 TAKIP — AKILLI PARA",
}
KIND_LABEL = {
    AlertKind.CONFLUENCE.value: "konfluans",
    AlertKind.VOLUME.value: "hacim",
    AlertKind.WATCH_PRICE.value: "takip-fiyat",
    AlertKind.WATCH_SMART.value: "takip-akilli",
}


# --------------------------------------------------------------------------- #
#  Ortak parcalar
# --------------------------------------------------------------------------- #
def age_str(since: datetime | None) -> str:
    if since is None:
        return "—"
    mins = (utcnow() - since).total_seconds() / 60
    if mins < 60:
        return f"{mins:.0f}dk"
    if mins < 1440:
        return f"{mins / 60:.1f}s"
    return f"{mins / 1440:.1f}g"


def dexscreener_url(tok: Token) -> str:
    return f"https://dexscreener.com/{tok.chain}/{tok.pair_address or tok.address}"


def explorer_url(tok: Token) -> str:
    return EXPLORER.get(tok.chain, "https://dexscreener.com/{a}").format(a=tok.address)


def market_lines(tok: Token) -> list[str]:
    """Her kartta ayni sirayla gecen piyasa bloku."""
    liq = tok.last_liquidity_usd
    buyable = tradeable_usd(liq)
    born = tok.pool_created_at or tok.first_seen_at
    lines = [
        f"MC {fmt_usd(tok.last_mc_usd)} · likidite {fmt_usd(liq)} · yas {age_str(born)}",
        f"1s hacim {fmt_usd(tok.last_volume_h1)} · 24s {fmt_usd(tok.last_volume_h24)}",
    ]
    # "Kagit uzerinde kat" uyarisi: 2 bin dolarlik havuzdaki 50x sana ait degil.
    lines.append(f"~%5 kaymayla girilebilir: <b>{fmt_usd(buyable)}</b>")
    return lines


def safety_line(tok: Token, payload: dict | None = None) -> str:
    score = (payload or {}).get("guvenlik_skoru")
    if score is None:
        score = tok.safety_score
    flags = (payload or {}).get("guvenlik_bayraklari") or tok.flags()
    if score is None:
        return "guvenlik: kontrol edilmedi"
    icon = "🟢" if score >= 0.7 else ("🟡" if score >= 0.45 else "🔴")
    line = f"guvenlik {icon} {score:.2f}"
    if flags:
        line += " — " + esc("; ".join(str(f) for f in flags[:3]))
    return line


def record_line(session, kind: str) -> str | None:
    """Bu sinyal tipinin GERCEK sicili. Kotu ise de yazilir."""
    st = journal.kind_stats(session, kind, days=30)
    if not st:
        return None
    rate = st["isabet_orani"]
    return (
        f"<i>sicil (30g): {st['kapali']} kart · isabet {fmt_pct(rate)} · "
        f"medyan tepe {fmt_mult(st['medyan_tepe'])} · medyan 24s {fmt_mult(st['medyan_24s'])}</i>"
    )


def ca_block(tok: Token) -> list[str]:
    return [
        "",
        f"<code>{esc(tok.address)}</code>",
        f"<a href=\"{dexscreener_url(tok)}\">grafik</a> · "
        f"<a href=\"{explorer_url(tok)}\">gezgin</a>",
    ]


def alert_keyboard(tok: Token, watched: bool) -> list[list[dict]]:
    row = [{"text": "📊 Rapor", "callback_data": f"t:{tok.id}"}]
    if watched:
        row.append({"text": "🔕 Sustur", "callback_data": f"wm:{tok.id}"})
        row.append({"text": "🗑 Cikar", "callback_data": f"wd:{tok.id}"})
    else:
        row.append({"text": "➕ Takibe al", "callback_data": f"wa:{tok.id}"})
    return [row]


# --------------------------------------------------------------------------- #
#  Kartlar
# --------------------------------------------------------------------------- #
def _header(tok: Token, kind: str) -> str:
    sym = esc(tok.symbol or tok.address[:6])
    return f"{KIND_TITLE.get(kind, '🔔')} — <b>{sym}</b>  <code>{esc(tok.chain)}</code>"


def confluence_card(session, al: Alert, tok: Token) -> list[str]:
    p = al.data()
    n = p.get("cuzdan_sayisi", 0)
    sellers = p.get("satan_cuzdan", 0)
    lines = [
        _header(tok, al.kind),
        "",
        f"<b>{n}</b> bagimsiz akilli cuzdan son <b>{p.get('pencere_saat', '?')} saatte</b> "
        f"bu tokenda ALIM yapti.",
    ]
    if sellers:
        # Zit yonlu veriyi gizlemek kartin degerini dusurur.
        lines.append(f"⚠ ayni pencerede <b>{sellers}</b> akilli cuzdan SATIS yapti.")
    if p.get("toplam_usd"):
        lines.append(f"toplam akilli alim: {fmt_usd(p['toplam_usd'])}")
    lines += ["", *market_lines(tok), safety_line(tok, p)]

    wallets = p.get("cuzdanlar") or []
    if wallets:
        lines += ["", "<b>Cuzdanlar</b> (adres gizli degil, sicili acik)"]
        for w in wallets[:5]:
            lines.append(
                f"· <code>{esc(w.get('kisa'))}</code> skor <b>{w.get('skor')}</b> · "
                f"isabet {fmt_pct(w.get('isabet'))} ({w.get('n')} olculmus alim)"
            )
    rec = record_line(session, al.kind)
    if rec:
        lines += ["", rec]
    lines += ca_block(tok)
    return lines


def volume_card(session, al: Alert, tok: Token) -> list[str]:
    p = al.data()
    lines = [
        _header(tok, al.kind),
        "",
        f"son 1 saat hacmi kendi 24s ortalamasinin <b>{p.get('kat', '?')}x</b> katinda",
        f"1s hacim {fmt_usd(p.get('hacim_1s'))} · normal {fmt_usd(p.get('ortalama_saatlik'))}",
        "",
        *market_lines(tok),
        safety_line(tok, p),
        "",
        "<i>Tek basina alim sebebi degildir. Degeri, konfluansin uzerine "
        "geldiginde ortaya cikar.</i>",
    ]
    rec = record_line(session, al.kind)
    if rec:
        lines += ["", rec]
    lines += ca_block(tok)
    return lines


def watch_price_card(session, al: Alert, tok: Token) -> list[str]:
    p = al.data()
    pct = p.get("yuzde") or 0
    arrow = "🟢 ▲" if pct > 0 else "🔴 ▼"
    lines = [
        _header(tok, al.kind),
        "",
        f"{arrow} <b>%{abs(pct):.1f}</b> ({p.get('sure_dk', 0)} dakikada) — esik %{p.get('esik')}",
        f"fiyat {fmt_usd(al.entry_price_usd)} (referans {fmt_usd(p.get('referans'))})",
        "",
        *market_lines(tok),
    ]
    lines += ca_block(tok)
    return lines


def watch_smart_card(session, al: Alert, tok: Token) -> list[str]:
    p = al.data()
    alan, satan = p.get("alan", 0), p.get("satan", 0)
    yon = "birikim" if alan > satan else ("dagitim" if satan > alan else "karisik")
    lines = [
        _header(tok, al.kind),
        "",
        f"listendeki tokende akilli para hareketi: <b>{yon}</b>",
        f"alan {alan} cuzdan ({fmt_usd(p.get('alim_usd'))}) · "
        f"satan {satan} cuzdan ({fmt_usd(p.get('satim_usd'))})",
        "",
        *market_lines(tok),
    ]
    lines += ca_block(tok)
    return lines


_RENDERERS = {
    AlertKind.CONFLUENCE.value: confluence_card,
    AlertKind.VOLUME.value: volume_card,
    AlertKind.WATCH_PRICE.value: watch_price_card,
    AlertKind.WATCH_SMART.value: watch_smart_card,
}


def render_alert(session, al: Alert, tok: Token, watched: bool) -> tuple[str, list[list[dict]]]:
    fn = _RENDERERS.get(al.kind, volume_card)
    lines = fn(session, al, tok)
    lines += ["", "<i>Yatirim tavsiyesi degildir. Veri gosterir, karar senindir.</i>"]
    return "\n".join(lines), alert_keyboard(tok, watched)
