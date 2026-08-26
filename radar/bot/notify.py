"""Alarm gonderimi.

Sessiz saatler yalnizca IKINCIL kartlari (hacim, takip fiyat) susturur.
Konfluans her zaman gecer: gece 3'te uyanmaya deger tek sinyal odur.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from ..config import settings
from ..db import Alert, AlertKind, Token, Watch, session_scope, utcnow
from ..http import HttpClient
from . import cards
from .telegram import Telegram

log = logging.getLogger(__name__)

ALWAYS_SEND = {AlertKind.CONFLUENCE.value}


def in_quiet_hours() -> bool:
    rng = settings.quiet_range
    if rng is None:
        return False
    start, end = rng
    hour = utcnow().hour
    return start <= hour < end if start <= end else (hour >= start or hour < end)


async def send_alerts(alert_ids: list[int], http: HttpClient) -> int:
    if not alert_ids or not settings.alerts_enabled:
        return 0
    tg = Telegram()
    if not tg.enabled:
        log.info("%s alarm uretildi ama TELEGRAM_BOT_TOKEN yok", len(alert_ids))
        return 0

    quiet = in_quiet_hours()
    sent = 0
    for aid in alert_ids:
        with session_scope() as s:
            al = s.get(Alert, aid)
            if al is None:
                continue
            if quiet and al.kind not in ALWAYS_SEND:
                log.debug("sessiz saat: %s atlandi", al.kind)
                continue
            tok = s.get(Token, al.token_id)
            if tok is None:
                continue
            chat_id = al.chat_id or tg.chat_id
            if not chat_id:
                continue
            watched = bool(
                s.scalar(
                    select(Watch.id).where(Watch.chat_id == chat_id, Watch.token_id == tok.id)
                )
            )
            text, keyboard = cards.render_alert(s, al, tok, watched)

        if await tg.send(chat_id, text, http, keyboard):
            sent += 1
    return sent
