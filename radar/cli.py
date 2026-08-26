"""Komut satiri.

    python -m radar run       # botu calistir (Railway/VPS icin)
    python -m radar once      # tum isleri bir kez calistir
    python -m radar diag      # veri kaynaklarini test et — HAM yaniti gosterir
    python -m radar karne     # sicili terminalde yazdir
    python -m radar cuzdan    # akilli cuzdan kadrosu
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .config import settings
from .db import init_db, session_scope

log = logging.getLogger("radar")


def setup_logging(level: str | None = None) -> None:
    logging.basicConfig(
        level=getattr(logging, (level or settings.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
#  diag — "calisiyor mu" sorusunun tek dogru cevabi
# --------------------------------------------------------------------------- #
async def cmd_diag(verbose: bool) -> int:
    from .bot.telegram import Telegram
    from .http import HttpClient
    from .sources.dexscreener import DexScreener
    from .sources.geckoterminal import GeckoTerminal
    from .sources.safety import check_token

    results: list[dict] = []
    async with HttpClient() as http:
        gt = GeckoTerminal(http)
        ds = DexScreener(http)

        for chain in settings.chains:
            results.append(await gt.diagnose(chain))
        results.append(await ds.diagnose())

        # Guvenlik kaynaklari — USDC referansiyla
        rep = await check_token(
            http, "solana", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            liquidity_usd=5_000_000, mc_usd=50_000_000, age_minutes=100_000,
        )
        results.append({
            "kaynak": "guvenlik (rugcheck+goplus)",
            "ok": bool(rep.sources),
            "cevap_veren": rep.sources,
            "skor": round(rep.score, 2),
            "bayrak": rep.flags,
        })

        tg = Telegram()
        if tg.enabled:
            me = await tg.me(http)
            results.append({
                "kaynak": "telegram",
                "ok": bool(me),
                "bot": (me or {}).get("username"),
                "sohbet_id_ayarli": bool(tg.chat_id),
            })
        else:
            results.append({"kaynak": "telegram", "ok": False,
                            "hata": "TELEGRAM_BOT_TOKEN bos"})

    bad = 0
    print("\n=== VERI KAYNAKLARI ===")
    for r in results:
        ok = r.get("ok")
        icon = "OK  " if ok else "HATA"
        if not ok:
            bad += 1
        detail = {k: v for k, v in r.items() if k not in ("kaynak", "ok", "ornek")}
        print(f"[{icon}] {r['kaynak']:<28} {detail}")
        if verbose and r.get("ornek"):
            print(json.dumps(r["ornek"], indent=2, ensure_ascii=False)[:1500])

    print(f"\nveritabani: {settings.database_url.split('@')[-1]}")
    print(f"zincir    : {', '.join(settings.chains)}")
    print(f"\n{len(results) - bad}/{len(results)} kaynak calisiyor.")
    if bad:
        print("Hata veren kaynaklar bedava katmanda gecici olarak dusmus olabilir; "
              "birkac dakika sonra tekrar dene.")
    return 1 if bad == len(results) else 0


# --------------------------------------------------------------------------- #
#  Raporlar
# --------------------------------------------------------------------------- #
def cmd_karne(days: int) -> int:
    from .core import journal

    init_db()
    with session_scope() as s:
        card = journal.scorecard(s, days=days)
    g = card["genel"]
    print(f"\n=== KARNE (son {days} gun) ===")
    print(f"toplam kart : {g['toplam']}  (acik {g['acik']}, kapali {g['kapali']})")
    if g["kapali"]:
        rate = g["isabet_orani"]
        print(f"isabet      : {g['isabet']}/{g['kapali']} = %{(rate or 0) * 100:.0f}")
        print(f"zarar       : {g['zarar']}")
        print(f"medyan tepe : {g['medyan_tepe']}")
        print(f"medyan 24s  : {g['medyan_24s']}")
    for kind, st in card["tip"].items():
        rate = st["isabet_orani"]
        rate_s = f"%{rate * 100:.0f}" if rate is not None else "—"
        print(f"  · {kind:<14} {st['kapali']:>4} kapali · isabet {rate_s:>4} · "
              f"medyan tepe {st['medyan_tepe']}")
    return 0


def cmd_wallets(limit: int) -> int:
    from .core.wallets import smart_wallets

    init_db()
    with session_scope() as s:
        rows = smart_wallets(s, limit=limit)
    if not rows:
        print("Henuz akilli cuzdan kadrosu olusmadi.")
        return 0
    print(f"\n=== AKILLI CUZDANLAR ({len(rows)}) ===")
    for i, w in enumerate(rows, 1):
        print(
            f"{i:>2}. {w['kisa']:<14} skor {w['skor']:>5.1f} · "
            f"isabet {(w['isabet'] or 0) * 100:>3.0f}% ({w['kazanan']}/{w['n']}) · "
            f"wilson {w['wilson']:.2f} · medyan {w['medyan_kat']:.2f}x"
        )
    return 0


def cmd_score() -> int:
    from .core.wallets import run_scoring

    init_db()
    print(run_scoring())
    return 0


# --------------------------------------------------------------------------- #
#  Giris
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="radar", description="Akilli para konfluans botu")
    parser.add_argument("--log-level", default=None)
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("run", help="Botu surekli calistir")
    sub.add_parser("once", help="Tum isleri bir kez calistir")
    d = sub.add_parser("diag", help="Veri kaynaklarini test et")
    d.add_argument("-v", "--verbose", action="store_true", help="Ham yaniti da goster")
    k = sub.add_parser("karne", help="Sicili yazdir")
    k.add_argument("gun", nargs="?", type=int, default=30)
    c = sub.add_parser("cuzdan", help="Akilli cuzdanlari yazdir")
    c.add_argument("adet", nargs="?", type=int, default=15)
    sub.add_parser("skor", help="Cuzdan skorlamasini simdi calistir")
    sub.add_parser("init", help="Veritabani semasini kur")

    args = parser.parse_args(argv)
    setup_logging(args.log_level)
    cmd = args.cmd or "run"

    if cmd == "run":
        from .scheduler import run_forever

        try:
            asyncio.run(run_forever())
        except KeyboardInterrupt:
            print("\nkapatiliyor…")
        return 0
    if cmd == "once":
        from .scheduler import run_once

        out = asyncio.run(run_once())
        for name, result in out.items():
            print(f"[{name}] {result}")
        return 0
    if cmd == "diag":
        return asyncio.run(cmd_diag(args.verbose))
    if cmd == "karne":
        return cmd_karne(args.gun)
    if cmd == "cuzdan":
        return cmd_wallets(args.adet)
    if cmd == "skor":
        return cmd_score()
    if cmd == "init":
        init_db()
        print("sema kuruldu:", settings.database_url.split("@")[-1])
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
