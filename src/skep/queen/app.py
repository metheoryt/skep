from __future__ import annotations

import asyncio
import os

from aiogram import Bot, Dispatcher
from aiohttp import web

from skep.app import build_dispatcher
from skep.config import QueenConfig, load_queen_config
from skep.discovery import advertise
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.router import QueenRouter
from skep.queen.telegram_sink import QueenSink
from skep.telegram_gw import Gateway, build_bot
from skep.ws_transport import QueenWsServer


def build_queen(qcfg: QueenConfig) -> tuple[Bot, Dispatcher, web.Application, QueenRouter]:
    bot = build_bot(qcfg)
    gateway = Gateway(bot, qcfg)
    bk = Bookkeeping.open(qcfg.bookkeeping_db)
    sink = QueenSink(gateway, bk)
    router = QueenRouter(bk)
    app = web.Application()
    QueenWsServer(router, sink, qcfg.shared_secret).attach(app)
    dp = build_dispatcher(router, qcfg)
    return bot, dp, app, router


async def serve(qcfg: QueenConfig) -> None:
    bot, dp, app, _router = build_queen(qcfg)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, qcfg.listen_host, qcfg.listen_port)
    await site.start()

    handle = None
    if qcfg.advertise_mdns:
        # advertise on the loopback/LAN address; a real deploy sets listen_host
        adv_host = "127.0.0.1" if qcfg.listen_host in ("0.0.0.0", "") else qcfg.listen_host
        handle = await advertise(adv_host, qcfg.listen_port,
                                 public_url=qcfg.public_url)
    try:
        await dp.start_polling(bot)
    finally:
        if handle is not None:
            await handle.close()
        await runner.cleanup()


def run() -> None:
    asyncio.run(serve(load_queen_config(os.environ)))
