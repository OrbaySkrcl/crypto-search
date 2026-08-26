"""Telegram Bot API sarmalayici — uzun yoklama (long polling).

Webhook yok: alan adi, sertifika, sabit IP gerekmez. Boylece bot bir
Raspberry Pi'de de, bedava bir konteynerde de ayni sekilde calisir.
"""
from __future__ import annotations

import html
import logging

from ..config import settings
from ..http import HttpClient

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
BUCKET = "telegram"
MAX_LEN = 3800          # Telegram siniri 4096; pay birakiyoruz


def esc(v: object) -> str:
    return html.escape(str(v if v is not None else ""), quote=False)


def fmt_usd(v: float | None) -> str:
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1_000_000_000:
        return f"${v / 1_000_000_000:.2f}B"
    if a >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if a >= 1_000:
        return f"${v / 1_000:.1f}K"
    if a >= 1:
        return f"${v:.2f}"
    return f"${v:.6f}".rstrip("0").rstrip(".")


def fmt_mult(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.2f}x" if v < 10 else f"{v:.0f}x"


def fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"%{v * 100:.0f}"


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    """Uzun mesaji satir sinirindan boler."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.split("\n"):
        if size + len(line) + 1 > limit and buf:
            parts.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        parts.append("\n".join(buf))
    return parts


class Telegram:
    def __init__(self, token: str | None = None, chat_id: str | None = None) -> None:
        self.token = (token if token is not None else settings.telegram_bot_token) or ""
        self.chat_id = (chat_id if chat_id is not None else settings.telegram_chat_id) or ""

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _url(self, method: str) -> str:
        return f"{API}/bot{self.token}/{method}"

    async def send(
        self,
        chat_id: str,
        text: str,
        http: HttpClient,
        keyboard: list[list[dict]] | None = None,
        disable_preview: bool = True,
    ) -> bool:
        if not self.enabled or not chat_id:
            return False
        ok = True
        chunks = split_message(text)
        for i, chunk in enumerate(chunks):
            body: dict = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": disable_preview,
            }
            # Klavye yalnizca son parcaya
            if keyboard and i == len(chunks) - 1:
                body["reply_markup"] = {"inline_keyboard": keyboard}
            # http_post None doner ancak cagri BASARISIZ oldugunda; Telegram'in
            # bos bir 'result' dondurmesi hata degildir.
            res = await self.http_post("sendMessage", body, http)
            ok = ok and res is not None
        return ok

    async def broadcast(
        self, text: str, http: HttpClient, keyboard: list[list[dict]] | None = None
    ) -> bool:
        return await self.send(self.chat_id, text, http, keyboard)

    async def http_post(self, method: str, body: dict, http: HttpClient) -> dict | None:
        data = await http.post(self._url(method), bucket=BUCKET, json_body=body)
        if isinstance(data, dict) and data.get("ok"):
            return data.get("result")
        if data is not None:
            log.warning("telegram %s basarisiz: %s", method, str(data)[:200])
        return None

    async def get_updates(
        self, offset: int | None, http: HttpClient, timeout: int = 25
    ) -> list[dict]:
        """Uzun yoklama: Telegram mesaj gelene kadar (en fazla `timeout` sn)
        yaniti bekletir. Kisa yoklamaya gore ~40 kat daha az istek eder."""
        if not self.enabled:
            return []
        params = {"timeout": max(0, timeout), "limit": 40}
        if offset is not None:
            params["offset"] = offset
        data = await http.get(self._url("getUpdates"), bucket=BUCKET, params=params)
        if isinstance(data, dict) and data.get("ok"):
            return data.get("result") or []
        return []

    async def answer_callback(self, callback_id: str, http: HttpClient, text: str = "") -> None:
        if not callback_id:
            return
        await self.http_post("answerCallbackQuery", {"callback_query_id": callback_id,
                                                     "text": text[:180]}, http)

    async def set_commands(self, commands: list[tuple[str, str]], http: HttpClient) -> None:
        await self.http_post(
            "setMyCommands",
            {"commands": [{"command": c, "description": d} for c, d in commands]},
            http,
        )

    async def me(self, http: HttpClient) -> dict | None:
        return await self.http_post("getMe", {}, http)
