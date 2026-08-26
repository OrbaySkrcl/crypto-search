"""Radar Telegram katmani ve kesif/fiyat dongusu — agsiz."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from radar.bot import handlers, notify
from radar.bot.poller import process_updates
from radar.bot.telegram import Telegram, esc, fmt_usd, split_message
from radar.core import discover, prefs, prices, repo, trades
from radar.db import Alert, AlertKind, PriceSnapshot, Token, Watch, session_scope, utcnow

CHAT = "999"
MINT = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
PAIR = "8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj"


# --------------------------------------------------------------------------- #
#  Bicimleme
# --------------------------------------------------------------------------- #
def test_address_extraction():
    assert handlers.extract_address(f"su coine bak {MINT} ne dersin") == MINT
    assert handlers.extract_address("0xdAC17F958D2ee523a2206206994597C13D831ec7") == \
        "0xdAC17F958D2ee523a2206206994597C13D831ec7"
    assert handlers.extract_address(f"https://dexscreener.com/solana/{MINT}") == MINT
    assert handlers.extract_address("merhaba nasilsin") is None


def test_usd_formatting_is_readable():
    assert fmt_usd(1_500_000) == "$1.50M"
    assert fmt_usd(84_000) == "$84.0K"
    assert fmt_usd(None) == "—"
    assert fmt_usd(0.00042).startswith("$0.0004")


def test_long_messages_are_split_on_line_breaks():
    text = "\n".join(f"satir {i}" * 20 for i in range(400))
    parts = split_message(text, limit=3800)
    assert len(parts) > 1
    assert all(len(p) <= 3800 for p in parts)
    assert sum(p.count("satir") for p in parts) == text.count("satir")


def test_html_is_escaped():
    assert esc("<script>") == "&lt;script&gt;"


# --------------------------------------------------------------------------- #
#  Takip listesi
# --------------------------------------------------------------------------- #
def _seed_token(symbol: str = "MOON", mc: float = 400_000.0) -> int:
    with session_scope() as s:
        tok, _ = repo.upsert_token(
            s, "solana", f"MINT{symbol}", symbol=symbol, pair_address=f"PAIR{symbol}",
            price_usd=mc / 1e9, mc_usd=mc, liquidity_usd=90_000,
            volume_h1=20_000, volume_h24=200_000,
            pool_created_at=utcnow() - timedelta(hours=8),
        )
        return tok.id


def test_watchlist_add_mute_remove(rdb):
    tid = _seed_token()

    assert "takibe alindi" in handlers.watch_add(CHAT, tid)
    assert "zaten listende" in handlers.watch_add(CHAT, tid)

    text, keyboard = handlers.cmd_list(CHAT)
    assert "MOON" in text and keyboard

    assert "susturuldu" in handlers.watch_mute(CHAT, tid)
    with session_scope() as s:
        assert s.scalar(select(Watch).where(Watch.token_id == tid)).muted is True
    assert "acildi" in handlers.watch_mute(CHAT, tid)

    assert "cikarildi" in handlers.watch_remove(CHAT, tid)
    assert "listende degil" in handlers.watch_remove(CHAT, tid)
    text, _ = handlers.cmd_list(CHAT)
    assert "bos" in text


def test_watchlist_is_per_chat(rdb):
    tid = _seed_token()
    handlers.watch_add(CHAT, tid)
    text, _ = handlers.cmd_list("other-chat")
    assert "bos" in text


def test_settings_roundtrip(rdb):
    assert prefs.get("esik_cuzdan") == 3
    out = handlers.cmd_settings("esik_cuzdan 5")
    assert "esik_cuzdan = 5" in out
    assert prefs.get("esik_cuzdan") == 5

    assert "❌" in handlers.cmd_settings("esik_cuzdan 99")     # ust sinir
    assert "❌" in handlers.cmd_settings("esik_cuzdan abc")    # sayi degil
    assert "❌" in handlers.cmd_settings("bilinmeyen 3")
    assert prefs.get("esik_cuzdan") == 5                       # bozulmadi
    handlers.cmd_settings("esik_cuzdan 3")


def test_empty_states_explain_themselves(rdb):
    assert "sinyal yok" in handlers.cmd_radar("24")
    assert "kadro" in handlers.cmd_wallets("")
    assert "kart gondermedim" in handlers.cmd_scorecard("")
    assert "Sistem durumu" in handlers.cmd_status()


# --------------------------------------------------------------------------- #
#  Kontrat adresi raporu
# --------------------------------------------------------------------------- #
DS_PAYLOAD = {
    "pairs": [{
        "chainId": "solana", "dexId": "raydium", "pairAddress": PAIR,
        "baseToken": {"address": MINT, "name": "Moon Token", "symbol": "MOON"},
        "priceUsd": "0.00042", "liquidity": {"usd": 84000.0},
        "marketCap": 410000, "volume": {"h24": 240000, "h1": 52000},
        "priceChange": {"h1": 12.4, "h24": -3.1},
        "txns": {"h1": {"buys": 210, "sells": 90}},
        "pairCreatedAt": 1740787200000,
    }]
}


@pytest.mark.asyncio
async def test_paste_contract_returns_full_report(rdb, fake_http):
    fake_http.add("/latest/dex/tokens/", DS_PAYLOAD)
    fake_http.add("/report/summary", {
        "score_normalised": 10,
        "risks": [{"name": "Low amount of LP Providers", "level": "warn"}],
    })

    text, keyboard = await handlers.handle(f"bak buna {MINT}", CHAT, fake_http)

    assert "MOON" in text
    assert "likidite $84.0K" in text
    assert "girilebilir" in text                    # gercekci giris buyuklugu
    assert "Guvenlik" in text
    assert "Akilli para" in text
    assert MINT in text
    assert any(b["callback_data"].startswith("wa:") for row in keyboard for b in row)

    with session_scope() as s:                       # token kaydedildi
        tok = repo.get_token(s, "solana", MINT)
        assert tok is not None and tok.safety_score is not None


@pytest.mark.asyncio
async def test_unknown_contract_says_so(rdb, fake_http):
    text, _ = await handlers.handle(MINT, CHAT, fake_http)
    assert "bulamadim" in text


@pytest.mark.asyncio
async def test_report_shows_past_calls(rdb, fake_http):
    """'Su tarihte demistik' cizgisi — bot kendi gecmisini tasir."""
    tid = _seed_token("MOON")
    with session_scope() as s:
        tok = s.get(Token, tid)
        tok.address = MINT
        s.add(Alert(
            kind=AlertKind.CONFLUENCE.value, token_id=tid, dedupe_key="past-1",
            created_at=utcnow() - timedelta(days=2), entry_mc_usd=100_000,
        ))
    fake_http.add("/latest/dex/tokens/", DS_PAYLOAD)
    text, _ = await handlers.handle(MINT, CHAT, fake_http)
    assert "Gecmiste ne demistim" in text
    assert "konfluans" in text


# --------------------------------------------------------------------------- #
#  Yoklama dongusu
# --------------------------------------------------------------------------- #
def _updates(*msgs):
    return {"ok": True, "result": list(msgs)}


def _msg(uid: int, text: str, chat: str = CHAT):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


@pytest.mark.asyncio
async def test_only_owner_chat_can_command(rdb, fake_http):
    fake_http.add("/getUpdates", _updates(_msg(1, "/durum", chat="12345")))
    fake_http.add("/sendMessage", {"ok": True, "result": {"message_id": 1}})

    tg = Telegram(token="test-token", chat_id=CHAT)
    handled = await process_updates(fake_http, tg)

    assert handled == 0
    sent = [c for c in fake_http.calls if "sendMessage" in c[1]]
    assert len(sent) == 1
    assert "ozeldir" in sent[0][2]["text"]


@pytest.mark.asyncio
async def test_owner_command_is_answered(rdb, fake_http):
    fake_http.add("/getUpdates", _updates(_msg(7, "/durum")))
    fake_http.add("/sendMessage", {"ok": True, "result": {"message_id": 1}})

    tg = Telegram(token="test-token", chat_id=CHAT)
    assert await process_updates(fake_http, tg) == 1
    sent = [c for c in fake_http.calls if "sendMessage" in c[1]]
    assert "Sistem durumu" in sent[0][2]["text"]


@pytest.mark.asyncio
async def test_button_callback_adds_to_watchlist(rdb, fake_http):
    tid = _seed_token()
    fake_http.add("/getUpdates", _updates({
        "update_id": 9,
        "callback_query": {
            "id": "cb1", "data": f"wa:{tid}",
            "message": {"chat": {"id": CHAT}},
        },
    }))
    fake_http.add("/sendMessage", {"ok": True, "result": {"message_id": 1}})
    fake_http.add("/answerCallbackQuery", {"ok": True, "result": True})

    tg = Telegram(token="test-token", chat_id=CHAT)
    assert await process_updates(fake_http, tg) == 1
    with session_scope() as s:
        assert s.scalar(select(Watch).where(Watch.token_id == tid)) is not None


@pytest.mark.asyncio
async def test_broken_command_does_not_kill_the_loop(rdb, fake_http, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("patladi")

    monkeypatch.setattr(handlers, "handle", boom)
    fake_http.add("/getUpdates", _updates(_msg(11, "/durum")))
    fake_http.add("/sendMessage", {"ok": True, "result": {"message_id": 1}})

    tg = Telegram(token="test-token", chat_id=CHAT)
    assert await process_updates(fake_http, tg) == 1
    sent = [c for c in fake_http.calls if "sendMessage" in c[1]]
    assert "RuntimeError" in sent[0][2]["text"]


# --------------------------------------------------------------------------- #
#  Sessiz saatler
# --------------------------------------------------------------------------- #
def test_quiet_hours_range_parsing(monkeypatch):
    from radar.config import settings

    monkeypatch.setattr(settings, "quiet_hours_utc", "23-7")
    assert settings.quiet_range == (23, 7)
    monkeypatch.setattr(settings, "quiet_hours_utc", "")
    assert settings.quiet_range is None


@pytest.mark.asyncio
async def test_quiet_hours_mute_volume_but_never_confluence(rdb, fake_http, monkeypatch):
    tid = _seed_token()
    with session_scope() as s:
        s.add(Alert(kind=AlertKind.VOLUME.value, token_id=tid, dedupe_key="q1",
                    entry_mc_usd=400_000))
        s.add(Alert(kind=AlertKind.CONFLUENCE.value, token_id=tid, dedupe_key="q2",
                    entry_mc_usd=400_000))
        s.flush()
        ids = [a.id for a in s.scalars(select(Alert))]

    from radar.config import settings

    monkeypatch.setattr(settings, "alerts_enabled", True)
    monkeypatch.setattr(notify, "in_quiet_hours", lambda: True)
    monkeypatch.setattr("radar.bot.notify.Telegram",
                        lambda: Telegram(token="test-token", chat_id=CHAT))
    fake_http.add("/sendMessage", {"ok": True, "result": {"message_id": 1}})

    sent = await notify.send_alerts(ids, fake_http)
    assert sent == 1
    bodies = [c[2]["text"] for c in fake_http.calls if "sendMessage" in c[1]]
    assert any("KONFLUANS" in b for b in bodies)
    assert not any("SIRA DISI HACIM" in b for b in bodies)


# --------------------------------------------------------------------------- #
#  Kesif ve fiyat dongusu
# --------------------------------------------------------------------------- #
def _pool(addr: str, liq: float, mc: float = 300_000.0):
    return {
        "id": f"solana_PAIR{addr}",
        "attributes": {
            "name": f"{addr} / SOL", "address": f"PAIR{addr}",
            "base_token_price_usd": "0.0003", "reserve_in_usd": str(liq),
            "market_cap_usd": str(mc),
            "pool_created_at": (utcnow() - timedelta(hours=2)).isoformat(),
            "volume_usd": {"h1": "20000", "h24": "150000"},
        },
        "relationships": {"base_token": {"data": {"id": f"solana_MINT{addr}"}},
                          "dex": {"data": {"id": "raydium"}}},
    }


@pytest.mark.asyncio
async def test_discover_filters_untradeable_pools(rdb, fake_http):
    fake_http.add("/new_pools", {"data": [_pool("DEEP", 60_000), _pool("THIN", 900)]})
    fake_http.add("/trending_pools", {"data": []})

    stats = await discover.discover_once(fake_http)

    assert stats["yeni"] == 1
    assert stats["elenen"] == 1
    with session_scope() as s:
        assert repo.get_token(s, "solana", "MINTDEEP") is not None
        assert repo.get_token(s, "solana", "MINTTHIN") is None


@pytest.mark.asyncio
async def test_price_refresh_builds_own_history(rdb, fake_http):
    _seed_token("MOON")
    fake_http.add("/latest/dex/tokens/", {
        "pairs": [{
            "chainId": "solana", "dexId": "raydium", "pairAddress": "PAIRMOON",
            "baseToken": {"address": "MINTMOON", "symbol": "MOON"},
            "priceUsd": "0.0009", "liquidity": {"usd": 95000},
            "marketCap": 900000, "volume": {"h24": 400000, "h1": 60000},
        }]
    })

    stats = await prices.refresh_prices(fake_http)
    assert stats["guncellenen"] == 1

    with session_scope() as s:
        tok = repo.get_token(s, "solana", "MINTMOON")
        assert tok.last_mc_usd == pytest.approx(900_000)
        snaps = list(s.scalars(select(PriceSnapshot).where(PriceSnapshot.token_id == tok.id)))
        assert len(snaps) == 1 and snaps[0].mc_usd == pytest.approx(900_000)


@pytest.mark.asyncio
async def test_price_refresh_marks_rug(rdb, fake_http):
    _seed_token("RUGME")
    fake_http.add("/latest/dex/tokens/", {
        "pairs": [{
            "chainId": "solana", "pairAddress": "PAIRRUGME",
            "baseToken": {"address": "MINTRUGME", "symbol": "RUGME"},
            "priceUsd": "0.0000001", "liquidity": {"usd": 200},
            "marketCap": 100, "volume": {"h24": 10, "h1": 1},
        }]
    })
    stats = await prices.refresh_prices(fake_http)
    assert stats["rug"] == 1
    with session_scope() as s:
        tok = repo.get_token(s, "solana", "MINTRUGME")
        assert tok.active is False


@pytest.mark.asyncio
async def test_trade_sampling_records_wallets(rdb, fake_http):
    _seed_token("MOON")
    fake_http.add("/trades", {"data": [
        {"attributes": {"tx_hash": "tx1", "tx_from_address": "W1", "kind": "buy",
                        "block_timestamp": utcnow().isoformat(),
                        "volume_in_usd": "800", "price_to_in_usd": "0.0004"}},
        {"attributes": {"tx_hash": "tx2", "tx_from_address": "W2", "kind": "sell",
                        "block_timestamp": utcnow().isoformat(),
                        "volume_in_usd": "600", "price_from_in_usd": "0.0004"}},
        {"attributes": {"tx_hash": "tx3", "tx_from_address": "W3", "kind": "buy",
                        "block_timestamp": utcnow().isoformat(),
                        "volume_in_usd": "10", "price_to_in_usd": "0.0004"}},
    ]})

    stats = await trades.sample_trades(fake_http)
    assert stats["yeni_islem"] == 2          # 10 dolarlik alim gurultu, elendi

    # Ayni veriyi tekrar cekmek yeni kayit uretmemeli
    stats2 = await trades.sample_trades(fake_http)
    assert stats2["yeni_islem"] == 0
