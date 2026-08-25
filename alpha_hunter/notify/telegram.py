"""Telegram bildirim katmani. Bot token + chat id yeter."""
from __future__ import annotations

import html
import logging

from ..config import settings
from ..http import HttpClient

log = logging.getLogger(__name__)
_API = "https://api.telegram.org"


class TelegramNotifier:
    def __init__(self, token: str | None = None, chat_id: str | None = None) -> None:
        self.token = token or settings.telegram_bot_token
        self.chat_id = chat_id or settings.telegram_chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id and settings.alerts_enabled)

    async def send(self, text: str, http: HttpClient | None = None, silent: bool = False) -> bool:
        if not self.enabled:
            log.debug("telegram kapali (token/chat_id yok)")
            return False
        payload = {
            "chat_id": self.chat_id,
            "text": text[:4090],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": silent,
        }
        url = f"{_API}/bot{self.token}/sendMessage"
        if http is not None:
            res = await http.post(url, bucket="telegram", json_body=payload, max_retries=2)
        else:
            async with HttpClient() as h:
                res = await h.post(url, bucket="telegram", json_body=payload, max_retries=2)
        ok = bool(isinstance(res, dict) and res.get("ok"))
        if not ok:
            log.warning("telegram gonderimi basarisiz: %s", res)
        return ok

    async def send_to(
        self,
        chat_id: str | int,
        text: str,
        http: HttpClient | None = None,
        keyboard: list[list[dict]] | None = None,
        silent: bool = False,
    ) -> bool:
        """Belirli bir sohbete gonderir (bot komutlarina cevap icin)."""
        if not self.token:
            return False
        payload: dict = {
            "chat_id": chat_id,
            "text": text[:4090],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": silent,
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        url = f"{_API}/bot{self.token}/sendMessage"
        if http is not None:
            res = await http.post(url, bucket="telegram", json_body=payload, max_retries=2)
        else:
            async with HttpClient() as h:
                res = await h.post(url, bucket="telegram", json_body=payload, max_retries=2)
        return bool(isinstance(res, dict) and res.get("ok"))

    # ------------------------------------------------------------------ #
    #  Bot tarafi: komutlari dinleme
    # ------------------------------------------------------------------ #
    async def get_updates(self, offset: int | None, http: HttpClient, timeout: int = 25) -> list[dict]:
        """Uzun yoklama (long polling). Webhook gerektirmez, Railway'de calisir."""
        if not self.token:
            return []
        params = {"timeout": str(timeout), "allowed_updates": '["message","callback_query"]'}
        if offset is not None:
            params["offset"] = str(offset)
        res = await http.get(
            f"{_API}/bot{self.token}/getUpdates",
            bucket="telegram_poll",
            params=params,
            max_retries=0,
        )
        if isinstance(res, dict) and res.get("ok"):
            return res.get("result") or []
        return []

    async def set_commands(self, commands: list[tuple[str, str]], http: HttpClient | None = None) -> bool:
        """Telegram'daki '/' kisayol menusunu kurar."""
        if not self.token:
            return False
        payload = {"commands": [{"command": c, "description": d} for c, d in commands]}
        url = f"{_API}/bot{self.token}/setMyCommands"
        if http is not None:
            res = await http.post(url, bucket="telegram", json_body=payload, max_retries=1)
        else:
            async with HttpClient() as h:
                res = await h.post(url, bucket="telegram", json_body=payload, max_retries=1)
        return bool(isinstance(res, dict) and res.get("ok"))

    async def answer_callback(self, callback_id: str, http: HttpClient, text: str = "") -> None:
        """Butona basildiginda Telegram'daki bekleme animasyonunu kapatir."""
        if not self.token:
            return
        await http.post(
            f"{_API}/bot{self.token}/answerCallbackQuery",
            bucket="telegram",
            json_body={"callback_query_id": callback_id, "text": text[:180]},
            max_retries=1,
        )

    async def check(self) -> str:
        """getMe ile yapilandirmayi dogrular."""
        if not self.token:
            return "TELEGRAM_BOT_TOKEN tanimli degil"
        async with HttpClient() as h:
            res = await h.get(f"{_API}/bot{self.token}/getMe", bucket="telegram", max_retries=1)
        if isinstance(res, dict) and res.get("ok"):
            u = res["result"]
            return f"baglandi: @{u.get('username')} (id {u.get('id')})"
        return "token gecersiz veya aga erisilemiyor"


def esc(s: object) -> str:
    return html.escape(str(s), quote=False)


def fmt_usd(v: float | None) -> str:
    if v is None:
        return "?"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"${v/div:.2f}{unit}"
    return f"${v:,.0f}"


def fmt_mult(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:.2f}x"


TIER_EMOJI = {"S": "🟣", "A": "🟢", "B": "🔵", "C": "🟡", "D": "🟠", "F": "🔴", "UNRATED": "⚪"}
