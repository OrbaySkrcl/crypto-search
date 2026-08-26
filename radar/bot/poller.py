"""Telegram uzun yoklama dongusu.

GUVENLIK: yalnizca TELEGRAM_CHAT_ID'deki sohbet komut verebilir. Botun
kullanici adini bulan biri kuyruga is birakamaz, veri sizdiramaz.
"""
from __future__ import annotations

import logging

from ..db import get_state, set_state
from ..http import HttpClient
from . import handlers
from .telegram import Telegram, esc

log = logging.getLogger(__name__)

_OFFSET_KEY = "telegram_offset"


def _authorised(chat_id: str, allowed: str) -> bool:
    return bool(allowed) and str(chat_id) == str(allowed)


async def process_updates(
    http: HttpClient, tg: Telegram | None = None, poll_timeout: int = 25
) -> int:
    tg = tg or Telegram()
    if not tg.enabled:
        return 0

    raw = get_state(_OFFSET_KEY)
    offset = int(raw) if raw and raw.lstrip("-").isdigit() else None
    updates = await tg.get_updates(offset, http, timeout=poll_timeout)
    handled = 0

    for up in updates:
        set_state(_OFFSET_KEY, str(up["update_id"] + 1))

        cb = up.get("callback_query") or {}
        msg = up.get("message") or up.get("edited_message") or {}

        if cb:
            chat_id = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
            await tg.answer_callback(cb.get("id", ""), http)
            if not _authorised(chat_id, tg.chat_id):
                continue
            body = cb.get("data", "")
        else:
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            body = (msg.get("text") or "").strip()
            if not chat_id or not body:
                continue
            if not _authorised(chat_id, tg.chat_id):
                log.warning("yetkisiz sohbet: %s", chat_id)
                await tg.send(
                    chat_id,
                    "Bu bot ozeldir; yalnizca sahibinin sohbetinde calisir.\n\n"
                    f"Sohbet kimligin: <code>{esc(chat_id)}</code>",
                    http,
                )
                continue

        try:
            text, keyboard = await handlers.handle(body, chat_id, http)
        except Exception as exc:                                        # noqa: BLE001
            log.exception("komut hatasi: %s", body[:60])
            text, keyboard = (
                f"Komut calistirilamadi: <code>{esc(type(exc).__name__)}</code>",
                handlers.MAIN_KEYBOARD,
            )
        await tg.send(chat_id, text, http, keyboard)
        handled += 1

    return handled
