"""Telegram komut botu testleri (agsiz)."""
import pytest

from alpha_hunter.config import settings
from alpha_hunter.db.models import Account, AppState, Job
from alpha_hunter.db.session import session_scope
from alpha_hunter.notify import bot
from alpha_hunter.notify.telegram import TelegramNotifier

CHAT = "123456789"


class FakeNotifier(TelegramNotifier):
    """Gonderilen mesajlari yakalar, aga cikmaz."""

    def __init__(self, updates=None):
        super().__init__(token="test-token", chat_id=CHAT)
        self.sent: list[tuple[str, str, object]] = []
        self._updates = updates or []
        self.commands_set = None

    async def get_updates(self, offset, http, timeout=25):
        out = [u for u in self._updates if offset is None or u["update_id"] >= offset]
        self._updates = []
        return out

    async def send_to(self, chat_id, text, http=None, keyboard=None, silent=False):
        self.sent.append((str(chat_id), text, keyboard))
        return True

    async def answer_callback(self, callback_id, http, text=""):
        return None

    async def set_commands(self, commands, http=None):
        self.commands_set = commands
        return True


def _msg(text, chat=CHAT, uid=1):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


@pytest.fixture()
def tg(db, monkeypatch):
    monkeypatch.setattr(settings, "telegram_chat_id", CHAT)
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    return FakeNotifier


# ------------------------------------------------------------------ komutlar
async def test_start_replies_with_menu(tg):
    n = tg([_msg("/start")])
    assert await bot.process_updates(n, None) == 1
    chat, text, kb = n.sent[0]
    assert chat == CHAT
    assert "Alpha Hunter" in text
    assert kb == bot.MAIN_KEYBOARD


async def test_help_lists_every_command(tg):
    n = tg([_msg("/yardim")])
    await bot.process_updates(n, None)
    text = n.sent[0][1]
    for cmd, _ in bot.COMMANDS:
        assert f"/{cmd}" in text


async def test_unknown_command_is_handled(tg):
    n = tg([_msg("/hebele")])
    await bot.process_updates(n, None)
    assert "Bilinmeyen komut" in n.sent[0][1]


async def test_top_on_empty_db_explains_the_wait(tg):
    n = tg([_msg("/top")])
    await bot.process_updates(n, None)
    assert "Henuz skorlanmis hesap yok" in n.sent[0][1]


async def test_status_command_reports_counts(tg):
    n = tg([_msg("/durum")])
    await bot.process_updates(n, None)
    text = n.sent[0][1]
    assert "Sistem durumu" in text
    assert "izlenen hesap" in text


# ---------------------------------------------------------------- /tara
async def test_scan_command_queues_job(tg):
    n = tg([_msg("/tara @SomeTrader 45")])
    await bot.process_updates(n, None)
    assert "kuyruga alindi" in n.sent[0][1]
    with session_scope() as s:
        job = s.query(Job).one()
        assert job.target == "sometrader"
        assert job.params == {"days": 45}
        assert job.source == "telegram"
        assert job.notify_chat_id == CHAT


async def test_scan_without_argument_shows_usage(tg):
    n = tg([_msg("/tara")])
    await bot.process_updates(n, None)
    assert "Kullanim" in n.sent[0][1]
    with session_scope() as s:
        assert s.query(Job).count() == 0


async def test_scan_rejects_invalid_handle(tg):
    n = tg([_msg("/tara bir!gecersiz")])
    await bot.process_updates(n, None)
    assert "gecerli bir hesap adi degil" in n.sent[0][1]


async def test_scan_defaults_to_sixty_days(tg):
    n = tg([_msg("/tara birhesap")])
    await bot.process_updates(n, None)
    with session_scope() as s:
        assert s.query(Job).one().params == {"days": 60}


# ------------------------------------------------------------- guvenlik
async def test_foreign_chat_cannot_run_commands(tg):
    n = tg([_msg("/tara kurban 60", chat="999999")])
    await bot.process_updates(n, None)
    assert "yalnizca sahibinin" in n.sent[0][1]
    with session_scope() as s:
        assert s.query(Job).count() == 0


async def test_no_chat_id_configured_blocks_everything(db, monkeypatch):
    monkeypatch.setattr(settings, "telegram_chat_id", "")
    n = FakeNotifier([_msg("/tara kurban")])
    await bot.process_updates(n, None)
    assert "yalnizca sahibinin" in n.sent[0][1]
    with session_scope() as s:
        assert s.query(Job).count() == 0


# ------------------------------------------------------------- mekanik
async def test_offset_is_persisted_so_updates_are_not_replayed(tg):
    n = tg([_msg("/durum", uid=42)])
    await bot.process_updates(n, None)
    with session_scope() as s:
        assert s.get(AppState, "telegram_update_offset").value == "43"


async def test_callback_button_is_routed(tg):
    n = tg([{
        "update_id": 7,
        "callback_query": {"id": "cb1", "data": "durum",
                           "message": {"chat": {"id": CHAT}}},
    }])
    await bot.process_updates(n, None)
    assert "Sistem durumu" in n.sent[0][1]


async def test_plain_text_suggests_a_scan(tg):
    n = tg([_msg("elonmusk")])
    await bot.process_updates(n, None)
    assert "/tara elonmusk 60" in n.sent[0][1]


async def test_account_command_points_to_scan_when_unknown(tg):
    n = tg([_msg("/hesap yokboyle")])
    await bot.process_updates(n, None)
    assert "/tara yokboyle 60" in n.sent[0][1]


async def test_account_command_reports_known_account(tg):
    with session_scope() as s:
        s.add(Account(platform="x", handle="bilinen", followers=100))
    n = tg([_msg("/hesap @bilinen")])
    await bot.process_updates(n, None)
    assert "@bilinen" in n.sent[0][1]


def test_command_menu_is_well_formed():
    assert len(bot.COMMANDS) >= 6
    for name, desc in bot.COMMANDS:
        assert name.islower() and name.isalpha()
        assert 3 <= len(desc) <= 256          # Telegram siniri
    names = [c for c, _ in bot.COMMANDS]
    assert len(names) == len(set(names))
