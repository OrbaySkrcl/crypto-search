"""Alpha Hunter komut satiri arayuzu.

    python -m alpha_hunter <komut> [secenekler]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import timedelta

from sqlalchemy import func, select

from .config import settings
from .db.models import Account, AccountCluster, Call, Token, Tweet, utcnow
from .db.session import healthcheck, init_db, session_scope
from .http import HttpClient

log = logging.getLogger("alpha_hunter")

try:
    from rich.console import Console
    from rich.table import Table
    _console = Console()
except ImportError:  # rich yoksa duz metne duser
    _console = None
    Table = None  # type: ignore


def _print(msg: str = "") -> None:
    if _console:
        _console.print(msg)
    else:
        print(_strip(msg))


def _strip(s: str) -> str:
    import re
    return re.sub(r"\[/?[a-z0-9_ .#]+\]", "", s)


def setup_logging(level: str | None = None) -> None:
    logging.basicConfig(
        level=getattr(logging, (level or settings.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
#  Komutlar
# --------------------------------------------------------------------------- #
def cmd_initdb(args: argparse.Namespace) -> int:
    init_db(drop=args.drop)
    _print("[green]veritabani semasi hazir[/green]")
    return 0


async def cmd_doctor(args: argparse.Namespace) -> int:
    from .ingest.collector import build_sources
    from .notify.telegram import TelegramNotifier

    rows: list[tuple[str, str, str]] = []
    rows.append(("veritabani", "OK" if healthcheck() else "HATA", settings.database_url.split("@")[-1]))
    rows.append(("zincirler", "OK", ", ".join(settings.chain_list)))

    async with HttpClient() as http:
        probe = "So11111111111111111111111111111111111111112"
        data = await http.get(f"{settings.dexscreener_base}/latest/dex/tokens/{probe}", bucket="dexscreener")
        rows.append(("dexscreener", "OK" if data else "HATA", "anahtar gerekmez"))

        gt = await http.get(
            f"{settings.geckoterminal_base}/networks", bucket="geckoterminal",
            headers={"Accept": "application/json;version=20230302"},
        )
        rows.append(("geckoterminal", "OK" if gt else "HATA", "bedava tarihsel OHLCV"))

        rows.append((
            "birdeye",
            "AKTIF" if settings.birdeye_api_key else "kapali",
            "BIRDEYE_API_KEY ile hassasiyet artar",
        ))
        rows.append((
            "helius",
            "AKTIF" if settings.helius_api_key else "public RPC",
            settings.solana_rpc_url if not settings.helius_api_key else "helius rpc",
        ))

        for src in build_sources(http):
            ok = await src.available()
            rows.append((f"kaynak:{src.name}", "AKTIF" if ok else "kapali", ""))

        if settings.nitter_list:
            alive = 0
            for inst in settings.nitter_list[:5]:
                body = await http.get(f"{inst}/elonmusk/rss", bucket="nitter",
                                      expect_json=False, max_retries=0)
                if body and body.lstrip().startswith("<?xml"):
                    alive += 1
            rows.append(("nitter ornekleri", f"{alive}/{len(settings.nitter_list[:5])} ayakta",
                         "0 ise APIFY_TOKEN ekle"))

    tg = TelegramNotifier()
    rows.append(("telegram", "AKTIF" if tg.enabled else "kapali", await tg.check() if tg.token else "token yok"))

    if Table:
        t = Table(title="Alpha Hunter — sistem kontrolu", show_lines=False)
        t.add_column("bilesen")
        t.add_column("durum")
        t.add_column("not", style="dim")
        for a, b, c in rows:
            style = "green" if b in ("OK", "AKTIF") else ("red" if b == "HATA" else "yellow")
            t.add_row(a, f"[{style}]{b}[/{style}]", c)
        _console.print(t)
    else:
        for a, b, c in rows:
            print(f"{a:22} {b:12} {c}")
    return 0


async def cmd_ingest(args: argparse.Namespace) -> int:
    from .pipeline.ingest import run_ingest
    stats = await run_ingest(lookback_minutes=args.lookback, limit=args.limit)
    _print(f"[green]{stats}[/green]")
    return 0


async def cmd_enrich(args: argparse.Namespace) -> int:
    from .pipeline.enrich import run_enrich
    total = {}
    for i in range(args.rounds):
        stats = await run_enrich(batch_size=args.batch)
        total = stats
        _print(f"tur {i+1}/{args.rounds}: {stats}")
        if stats["evaluated"] == 0:
            break
    _print(f"[green]{total}[/green]")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    from .pipeline.score import blacklist_spammers, detect_clusters, run_scoring
    if not args.no_filters:
        n = blacklist_spammers()
        c = detect_clusters()
        _print(f"spam filtresi: {n} hesap · koordinasyon: {c} kume")
    results = run_scoring()
    _print(f"[green]{len(results)} hesap skorlandi[/green]")
    return cmd_leaderboard(args)


def cmd_leaderboard(args: argparse.Namespace) -> int:
    from .scoring.engine import latest_scores
    limit = getattr(args, "limit", 20) or 20
    with session_scope() as s:
        rows = latest_scores(s, limit=limit, min_tier=getattr(args, "tier", None))
        if not rows:
            _print("[yellow]henuz skor yok — once `ingest` ve `enrich` calistir[/yellow]")
            return 0
        data = []
        for r in rows:
            acc = s.get(Account, r.account_id)
            data.append((
                acc.handle if acc else "?",
                r.tier.value if hasattr(r.tier, "value") else str(r.tier),
                r.alpha_score, r.win_rate, r.n_wins, r.n_evaluated, r.median_multiple,
                r.entry_quality, r.originality, r.calls_per_day,
                bool(acc and acc.cluster_id),
            ))

    if Table:
        t = Table(title=f"ALPHA LEADERBOARD (son {settings.score_window_days} gun)")
        for c, j in [("#", "right"), ("hesap", "left"), ("tier", "center"), ("alfa", "right"),
                     ("win", "right"), ("n", "right"), ("medyan", "right"), ("giris", "right"),
                     ("ozgun", "right"), ("cagri/gun", "right"), ("kume", "center")]:
            t.add_column(c, justify=j)
        for i, d in enumerate(data, 1):
            colour = {"S": "magenta", "A": "green", "B": "cyan", "C": "yellow",
                      "D": "orange3", "F": "red"}.get(d[1], "white")
            t.add_row(
                str(i), f"@{d[0]}", f"[{colour}]{d[1]}[/{colour}]", f"{d[2]:.1f}",
                f"{d[3]*100:.0f}%", f"{d[4]}/{d[5]}", f"{d[6]:.1f}x",
                f"{d[7]:.2f}", f"{d[8]:.2f}", f"{d[9]:.1f}", "⚠" if d[10] else "",
            )
        _console.print(t)
        _console.print(
            "[dim]alfa = 0.32·guvenilirlik + 0.24·buyukluk + 0.20·giris kalitesi + "
            "0.12·hayatta kalma + 0.12·ozgunluk, spray ve tutarlilik carpanlariyla[/dim]"
        )
    else:
        for i, d in enumerate(data, 1):
            print(f"{i:3}. @{d[0]:<20} {d[1]:<7} {d[2]:6.1f}  win {d[3]*100:3.0f}% "
                  f"({d[4]}/{d[5]})  medyan {d[6]:.1f}x  {d[9]:.1f}/gun")
    return 0


async def cmd_backfill(args: argparse.Namespace) -> int:
    from .pipeline.backfill import backfill_many
    handles = [h.strip() for h in args.handles.split(",") if h.strip()]
    results = await backfill_many(handles, days=args.days)
    if not results:
        _print("[yellow]hicbir hesap icin cagri bulunamadi[/yellow]")
        return 0
    for r in results:
        _print(
            f"[bold]@{r.handle}[/bold]  alfa [bold]{r.alpha_score:.1f}[/bold] ({r.tier})  "
            f"win {r.win_rate*100:.0f}% ({r.n_wins}/{r.n_evaluated})  "
            f"medyan {r.median_multiple:.2f}x  giris kalitesi {r.entry_quality:.2f}  "
            f"{r.calls_per_day:.1f} cagri/gun"
        )
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    handle = args.handle.lstrip("@").lower()
    with session_scope() as s:
        acc = s.scalar(select(Account).where(Account.handle == handle))
        if acc is None:
            _print(f"[red]@{handle} veritabaninda yok[/red]")
            return 1
        calls = list(
            s.scalars(
                select(Call).where(Call.account_id == acc.id).order_by(Call.called_at.desc()).limit(args.limit)
            )
        )
        _print(f"\n[bold]@{acc.handle}[/bold] · {acc.followers or '?'} takipci · "
               f"{len(calls)} cagri gosteriliyor"
               + (f" · [red]KARA LISTE: {acc.blacklist_reason}[/red]" if acc.is_blacklisted else ""))
        if acc.cluster_id:
            cl = s.get(AccountCluster, acc.cluster_id)
            _print(f"[yellow]⚠ koordineli kume: {cl.label if cl else acc.cluster_id}[/yellow]")

        rows = []
        for c in calls:
            tok = s.get(Token, c.token_id)
            rows.append((
                c.called_at.strftime("%m-%d %H:%M"),
                (tok.symbol or tok.address[:8]) if tok else "?",
                c.entry_mc_usd, c.max_multiple, c.sustained_multiple,
                c.entry_quality, c.run_capture, c.caller_rank,
                c.outcome.value if hasattr(c.outcome, "value") else str(c.outcome),
                c.entry_confidence,
            ))

    from .notify.telegram import fmt_usd
    if Table:
        t = Table(title=f"@{handle} — cagri gecmisi")
        for col, j in [("zaman", "left"), ("token", "left"), ("giris MC", "right"),
                       ("ATH x", "right"), ("gercekci x", "right"), ("giris kal.", "right"),
                       ("capture", "right"), ("sira", "right"), ("sonuc", "center"),
                       ("guven", "right")]:
            t.add_column(col, justify=j)
        for r in rows:
            oc = {"win": "green", "loss": "red", "rug": "red", "pending": "yellow",
                  "invalid": "dim"}.get(r[8], "white")
            t.add_row(
                r[0], str(r[1]), fmt_usd(r[2]),
                f"{r[3]:.2f}" if r[3] else "-", f"{r[4]:.2f}" if r[4] else "-",
                f"{r[5]:.2f}" if r[5] is not None else "-",
                f"{r[6]:.2f}" if r[6] is not None else "-",
                str(r[7] or "-"), f"[{oc}]{r[8]}[/{oc}]", f"{r[9]:.2f}",
            )
        _console.print(t)
    else:
        for r in rows:
            print(r)
    return 0


async def cmd_token(args: argparse.Namespace) -> int:
    from .notify.telegram import fmt_usd
    from .oracle.resolver import PriceOracle
    from .security.checks import SecurityChecker

    async with HttpClient() as http:
        oracle = PriceOracle(http)
        info = None
        for chain in settings.chain_list:
            info = await oracle.token_info(chain, args.address)
            if info:
                break
        if info is None:
            _print("[red]token hicbir zincirde bulunamadi[/red]")
            return 1
        _print(f"\n[bold]{info.symbol or '?'}[/bold] — {info.name or ''}  ({info.chain})")
        _print(f"adres     : {info.address}")
        _print(f"fiyat     : ${info.price_usd:.10f}" if info.price_usd else "fiyat     : ?")
        _print(f"MC / FDV  : {fmt_usd(info.mc_usd)} / {fmt_usd(info.fdv_usd)}")
        _print(f"likidite  : {fmt_usd(info.liquidity_usd)}   24s hacim: {fmt_usd(info.volume_24h)}")
        _print(f"havuz     : {info.dex_id} · {info.pair_address}")
        _print(f"dogum     : {info.pair_created_at}")

        if info.chain == "solana":
            rep = await SecurityChecker(http).check(info.chain, info.address)
            _print(f"\n[bold]guvenlik[/bold] {rep.score*100:.0f}/100")
            _print(f"  mint yetkisi   : {'DEVREDILMIS' if rep.mint_renounced else rep.mint_authority}")
            _print(f"  freeze yetkisi : {'DEVREDILMIS' if rep.freeze_renounced else rep.freeze_authority}")
            if rep.top10_pct is not None:
                _print(f"  ilk 10 cuzdan  : %{rep.top10_pct*100:.1f}")
            if rep.flags:
                _print(f"  [red]bayraklar: {', '.join(rep.flags)}[/red]")

    with session_scope() as s:
        tok = s.scalar(select(Token).where(Token.address == args.address))
        if tok:
            calls = list(
                s.scalars(select(Call).where(Call.token_id == tok.id).order_by(Call.called_at.asc()))
            )
            if calls:
                _print(f"\n[bold]bu CA'yi cagiran {len(calls)} tweet[/bold]")
                for c in calls[:15]:
                    a = s.get(Account, c.account_id)
                    _print(f"  {c.called_at.strftime('%m-%d %H:%M')}  "
                           f"#{c.caller_rank or '?'}  @{a.handle if a else '?'}  "
                           f"giris {fmt_usd(c.entry_mc_usd)}  "
                           f"{(f'{c.max_multiple:.2f}x' if c.max_multiple else '-')}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with session_scope() as s:
        def n(model) -> int:
            return s.scalar(select(func.count()).select_from(model)) or 0
        outcomes = dict(
            s.execute(select(Call.outcome, func.count(Call.id)).group_by(Call.outcome)).all()
        )
        _print("\n[bold]VERITABANI DURUMU[/bold]")
        _print(f"  hesaplar : {n(Account)}")
        _print(f"  tweetler : {n(Tweet)}")
        _print(f"  tokenlar : {n(Token)}")
        _print(f"  cagrilar : {n(Call)}")
        for k, v in outcomes.items():
            key = k.value if hasattr(k, "value") else str(k)
            _print(f"     {key:9}: {v}")
        best = s.scalar(select(func.max(Call.max_multiple)))
        if best:
            _print(f"  en yuksek kat: {best:.1f}x")
        recent = s.scalar(
            select(func.count(Call.id)).where(Call.called_at >= utcnow() - timedelta(hours=24))
        )
        _print(f"  son 24 saatte {recent} cagri")
    return 0


async def cmd_alert(args: argparse.Namespace) -> int:
    from .notify.telegram import TelegramNotifier
    from .pipeline.alerts import alert_fresh_calls, send_leaderboard
    tg = TelegramNotifier()
    if args.test:
        ok = await tg.send("✅ <b>Alpha Hunter</b> baglanti testi basarili.")
        _print("[green]gonderildi[/green]" if ok else "[red]gonderilemedi[/red]")
        return 0 if ok else 1
    if args.leaderboard:
        ok = await send_leaderboard(args.limit or 15)
        _print("[green]liderlik tablosu gonderildi[/green]" if ok else "[red]gonderilemedi[/red]")
        return 0
    sent = await alert_fresh_calls(lookback_minutes=args.lookback)
    _print(f"{sent} alarm gonderildi")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .web.app import serve
    _print(f"[green]pano aciliyor:[/green] http://localhost:{args.port or settings.web_port}")
    if not settings.web_password:
        _print("[yellow]uyari: WEB_PASSWORD bos — pano sifresiz. Internete acacaksan doldur.[/yellow]")
    serve(host=args.host, port=args.port)
    return 0


async def cmd_run(args: argparse.Namespace) -> int:
    from .pipeline.scheduler import run_forever
    init_db()
    await run_forever()
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="alpha_hunter", description="Alpha Hunter — memecoin alfa hesap avcisi")
    p.add_argument("--log-level", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("initdb", help="veritabani semasini olustur")
    s.add_argument("--drop", action="store_true", help="once tum tablolari sil")
    s.set_defaults(fn=cmd_initdb, is_async=False)

    s = sub.add_parser("doctor", help="yapilandirma ve baglanti kontrolu")
    s.set_defaults(fn=cmd_doctor, is_async=True)

    s = sub.add_parser("ingest", help="tweet topla ve cagrilari kaydet")
    s.add_argument("--lookback", type=int, default=None, help="kac dakika geriye")
    s.add_argument("--limit", type=int, default=None, help="sorgu basina tweet siniri")
    s.set_defaults(fn=cmd_ingest, is_async=True)

    s = sub.add_parser("enrich", help="bekleyen cagrilari fiyatlandir")
    s.add_argument("--rounds", type=int, default=1)
    s.add_argument("--batch", type=int, default=60)
    s.set_defaults(fn=cmd_enrich, is_async=True)

    s = sub.add_parser("score", help="skorlari hesapla ve tabloyu goster")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--tier", default=None, help="minimum tier (S/A/B/C/D/F)")
    s.add_argument("--no-filters", action="store_true", help="spam/kume filtrelerini atla")
    s.set_defaults(fn=cmd_score, is_async=False)

    s = sub.add_parser("leaderboard", help="mevcut siralamayi goster")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--tier", default=None)
    s.set_defaults(fn=cmd_leaderboard, is_async=False)

    s = sub.add_parser("backfill", help="belirli hesaplarin gecmisini analiz et")
    s.add_argument("handles", help="virgullu handle listesi (@ olmadan)")
    s.add_argument("--days", type=int, default=60)
    s.set_defaults(fn=cmd_backfill, is_async=True)

    s = sub.add_parser("inspect", help="tek hesabin cagri gecmisi")
    s.add_argument("handle")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(fn=cmd_inspect, is_async=False)

    s = sub.add_parser("token", help="tek bir CA'yi incele")
    s.add_argument("address")
    s.set_defaults(fn=cmd_token, is_async=True)

    s = sub.add_parser("stats", help="veritabani ozeti")
    s.set_defaults(fn=cmd_stats, is_async=False)

    s = sub.add_parser("alert", help="telegram alarmlari")
    s.add_argument("--test", action="store_true")
    s.add_argument("--leaderboard", action="store_true")
    s.add_argument("--lookback", type=int, default=60)
    s.add_argument("--limit", type=int, default=15)
    s.set_defaults(fn=cmd_alert, is_async=True)

    s = sub.add_parser("serve", help="sadece web panosunu ac")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=None)
    s.set_defaults(fn=cmd_serve, is_async=False)

    s = sub.add_parser("run", help="surekli calis + web panosu (Railway)")
    s.set_defaults(fn=cmd_run, is_async=True)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    if args.cmd not in ("initdb", "doctor"):
        init_db()
    try:
        if args.is_async:
            return asyncio.run(args.fn(args))
        return args.fn(args)
    except KeyboardInterrupt:
        _print("\n[yellow]iptal edildi[/yellow]")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
