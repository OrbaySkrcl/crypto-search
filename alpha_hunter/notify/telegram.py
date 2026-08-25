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
