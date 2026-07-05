"""Shared queen-side mailbox assembly.

Houses the Telegram-facing MailboxService wiring so BOTH the split queen
(`skep.queen.app`) and the interim single-process app (`skep.app`) build it the
same way. Kept in its own module (rather than in either app module) because
`skep.queen.app` imports `build_dispatcher` from `skep.app`; putting these
helpers here avoids an import cycle. This module never imports `skep.app`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Protocol

from aiogram.exceptions import TelegramBadRequest
from aiohttp import web

from skep.config import QueenConfig
from skep.formatting import escape_md
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.mailbox import (
    Mailbox,
    MailboxService,
    Message,
    PermanentDeliveryError,
)
from skep.telegram_gw import Gateway

log = logging.getLogger(__name__)


class _Redeliverable(Protocol):
    async def redeliver_ceo(self) -> None: ...


async def _ceo_retry_loop(service: _Redeliverable, interval: float) -> None:
    """Periodically retry pending CEO mail.

    Runs a sweep immediately (draining anything left pending by a prior run),
    then every `interval` seconds. Never lets one failed sweep kill the loop.
    """
    while True:
        try:
            await service.redeliver_ceo()
        except Exception:
            log.warning("CEO retry sweep failed", exc_info=True)
        await asyncio.sleep(interval)


def _install_ceo_retry(
    app: web.Application, service: _Redeliverable, interval: float
) -> None:
    """Tie a CEO-retry background loop to the web app's lifecycle: it starts
    on AppRunner.setup() and is cancelled on AppRunner.cleanup()."""

    async def _ctx(app: web.Application) -> AsyncIterator[None]:
        task = asyncio.create_task(_ceo_retry_loop(service, interval))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app.cleanup_ctx.append(_ctx)


def make_ceo_callbacks(
    gateway: Gateway,
    topic_id: int | None,
) -> tuple[
    Callable[[Message], Awaitable[None]],
    Callable[[str], Awaitable[None]],
]:
    """Build the queen's Telegram-facing MailboxService callbacks.

    Kept out of mailbox.py so MailboxService itself stays Telegram-free.
    """

    async def deliver_ceo(msg: Message) -> None:
        text = (
            f"📬 *{escape_md(msg.sender)}* → you\n"
            f"*{escape_md(msg.subject)}*\n\n"
            f"{escape_md(msg.body)}\n\n"
            f"_reply id: {escape_md(str(msg.id))}_"
        )
        try:
            await gateway.post(topic_id, text)
        except TelegramBadRequest as exc:
            # 400s never succeed on retry (e.g. body over Telegram's 4096-char
            # limit -- mailbox_body_cap allows up to 16384 bytes). Mark it
            # permanent so redeliver_ceo dead-letters it instead of wedging the
            # CEO queue behind an un-sendable message.
            raise PermanentDeliveryError(str(exc)) from exc

    async def alert_ceo(text: str) -> None:
        await gateway.post(topic_id, escape_md(text))

    return deliver_ceo, alert_ceo


def _mailbox_db_path(bookkeeping_db: str) -> str:
    """Sibling DB next to the bookkeeping store (own connection/schema)."""
    if bookkeeping_db == ":memory:":
        return ":memory:"
    return str(Path(bookkeeping_db).with_name("mailbox.sqlite"))


def build_mailbox_service(
    qcfg: QueenConfig,
    gateway: Gateway,
    bk: Bookkeeping,
    mailbox: Mailbox,
    *,
    topic_id: int | None = None,
) -> MailboxService:
    """Assemble a MailboxService from queen config + Telegram callbacks.

    Single source of truth shared by build_queen (split path) and the interim
    single-process app, so the two never drift on policy knobs.
    """
    deliver_ceo, alert_ceo = make_ceo_callbacks(gateway, topic_id)
    return MailboxService(
        mailbox,
        bk,
        set(qcfg.managers),
        deliver_ceo,
        alert_ceo,
        rate_limit=qcfg.mailbox_rate_limit,
        rate_window=qcfg.mailbox_rate_window,
        depth_cap=qcfg.mailbox_depth_cap,
        dedupe_window=qcfg.mailbox_dedupe_window,
        body_cap=qcfg.mailbox_body_cap,
    )
