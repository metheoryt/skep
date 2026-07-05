from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Protocol

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ChatMemberUpdated
from aiohttp import web

from skep.app import build_dispatcher
from skep.config import QueenConfig, load_queen_config
from skep.discovery import advertise
from skep.formatting import escape_md
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.mailbox import (
    Mailbox,
    MailboxService,
    Message,
    PermanentDeliveryError,
)
from skep.queen.onboarding import onboard_group
from skep.queen.router import QueenRouter
from skep.queen.telegram_sink import QueenSink
from skep.telegram_gw import Gateway, build_bot
from skep.ws_transport import QueenWsServer

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


def build_queen(qcfg: QueenConfig) -> tuple[Bot, Dispatcher, web.Application, QueenRouter]:
    bot = build_bot(qcfg)
    gateway = Gateway(bot, qcfg)
    bk = Bookkeeping.open(qcfg.bookkeeping_db)
    mailbox = Mailbox.open(_mailbox_db_path(qcfg.bookkeeping_db))
    # No dedicated mailbox topic exists yet; post CEO mail/alerts to the
    # group's General topic (message_thread_id=None), same destination
    # used for other queen-level notices.
    deliver_ceo, alert_ceo = make_ceo_callbacks(gateway, None)
    mailbox_service = MailboxService(
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
    sink = QueenSink(gateway, bk, mailbox_service=mailbox_service)
    router = QueenRouter(bk)
    app = web.Application()
    QueenWsServer(router, sink, qcfg.shared_secret,
                  bookkeeping=bk, mailbox_service=mailbox_service).attach(app)
    if qcfg.mailbox_ceo_retry_interval > 0:
        _install_ceo_retry(app, mailbox_service,
                           qcfg.mailbox_ceo_retry_interval)
    dp = build_dispatcher(router, qcfg, mailbox_service=mailbox_service, mailbox=mailbox)

    @dp.my_chat_member()
    async def _on_added(event: ChatMemberUpdated) -> None:
        await onboard_group(bot, event.chat.id, qcfg.owner_id)

    return bot, dp, app, router


async def serve(qcfg: QueenConfig) -> None:
    if not qcfg.shared_secret.strip():
        raise SystemExit(
            "SKEP_SHARED_SECRET is required (worker<->queen auth); "
            "refusing to start without it")
    bot, dp, app, _router = build_queen(qcfg)
    runner = web.AppRunner(app)
    await runner.setup()

    handle = None
    try:
        site = web.TCPSite(runner, qcfg.listen_host, qcfg.listen_port)
        await site.start()

        if qcfg.advertise_mdns:
            # advertise on the loopback/LAN address; a real deploy sets listen_host
            adv_host = "127.0.0.1" if qcfg.listen_host in ("0.0.0.0", "") else qcfg.listen_host
            handle = await advertise(adv_host, qcfg.listen_port,
                                     public_url=qcfg.public_url)
        await dp.start_polling(bot)
    finally:
        if handle is not None:
            await handle.close()
        await runner.cleanup()


def run() -> None:
    asyncio.run(serve(load_queen_config(os.environ)))
