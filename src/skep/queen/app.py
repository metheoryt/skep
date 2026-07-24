from __future__ import annotations

import asyncio
import os

from aiogram import Bot, Dispatcher
from aiogram.types import ChatMemberUpdated
from aiohttp import web

from skep.app import build_dispatcher
from skep.config import QueenConfig, load_queen_config
from skep.discovery import advertise

# Re-exported for callers that historically imported these from this module
# (they now live in skep.queen.assembly); keeps those import sites working.
from skep.queen.assembly import (  # noqa: E402,F401
    _ceo_retry_loop,
    _install_ceo_retry,
    _install_park_sweep,
    _mailbox_db_path,
    build_mailbox_service,
    make_ceo_callbacks,
)
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.mailbox import Mailbox
from skep.queen.onboarding import onboard_group
from skep.queen.router import QueenRouter
from skep.queen.telegram_sink import QueenSink
from skep.telegram_gw import Gateway, build_bot
from skep.ws_transport import QueenWsServer


def build_queen(
    qcfg: QueenConfig,
) -> tuple[Bot, Dispatcher, web.Application, QueenRouter]:
    bot = build_bot(qcfg)
    gateway = Gateway(bot, qcfg)
    bk = Bookkeeping.open(qcfg.bookkeeping_db)
    mailbox = Mailbox.open(_mailbox_db_path(qcfg.bookkeeping_db))
    # No dedicated mailbox topic exists yet; post CEO mail/alerts to the
    # group's General topic (message_thread_id=None), same destination
    # used for other queen-level notices.
    mailbox_service = build_mailbox_service(qcfg, gateway, bk, mailbox)
    sink = QueenSink(
        gateway,
        bk,
        mailbox_service=mailbox_service,
        park_default_backoff=qcfg.park_default_backoff,
    )
    router = QueenRouter(bk)
    app = web.Application()
    QueenWsServer(
        router,
        sink,
        qcfg.shared_secret,
        bookkeeping=bk,
        mailbox_service=mailbox_service,
    ).attach(app)
    if qcfg.mailbox_ceo_retry_interval > 0:
        _install_ceo_retry(app, mailbox_service, qcfg.mailbox_ceo_retry_interval)
    if qcfg.park_sweep_interval > 0:
        _install_park_sweep(app, bk, router, qcfg.park_sweep_interval)
    dp = build_dispatcher(
        router, qcfg, mailbox_service=mailbox_service, mailbox=mailbox
    )

    @dp.my_chat_member()
    async def _on_added(event: ChatMemberUpdated) -> None:
        await onboard_group(bot, event.chat.id, qcfg.owner_id)

    return bot, dp, app, router


async def serve(qcfg: QueenConfig) -> None:
    if not qcfg.shared_secret.strip():
        raise SystemExit(
            "SKEP_SHARED_SECRET is required (worker<->queen auth); "
            "refusing to start without it"
        )
    bot, dp, app, _router = build_queen(qcfg)
    runner = web.AppRunner(app)
    await runner.setup()

    handle = None
    try:
        site = web.TCPSite(runner, qcfg.listen_host, qcfg.listen_port)
        await site.start()

        if qcfg.advertise_mdns:
            # advertise on the loopback/LAN address; a real deploy sets listen_host
            adv_host = (
                "127.0.0.1" if qcfg.listen_host in ("0.0.0.0", "") else qcfg.listen_host
            )
            handle = await advertise(
                adv_host, qcfg.listen_port, public_url=qcfg.public_url
            )
        await dp.start_polling(bot)
    finally:
        if handle is not None:
            await handle.close()
        await runner.cleanup()


def run() -> None:
    asyncio.run(serve(load_queen_config(os.environ)))
