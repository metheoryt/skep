from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.types import ChatMemberUpdated
from aiohttp import web

from skep.app import build_dispatcher
from skep.config import QueenConfig, load_queen_config
from skep.discovery import advertise
from skep.formatting import escape_md
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.mailbox import Mailbox, MailboxService, Message
from skep.queen.onboarding import onboard_group
from skep.queen.router import QueenRouter
from skep.queen.telegram_sink import QueenSink
from skep.telegram_gw import Gateway, build_bot
from skep.ws_transport import QueenWsServer


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
        await gateway.post(topic_id, text)

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
