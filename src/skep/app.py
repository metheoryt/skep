from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, TelegramObject

from skep.config import QueenConfig, WorkerConfig, load_queen_config, load_worker_config
from skep.db import Registry
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.router import QueenRouter, UnknownWorker
from skep.queen.telegram_sink import QueenSink
from skep.supervisor import CapacityError, Supervisor
from skep.telegram_gw import Gateway, build_bot, is_owner
from skep.transport import InMemoryEventSink


def parse_spawn(args: str) -> tuple[str, str, str, str] | None:
    """Parse `<host> [--profile <p>] <repo> <task...>` -> (host, profile, repo, task)."""
    tokens = (args or "").split()
    if len(tokens) < 3:
        return None
    host = tokens[0]
    rest = tokens[1:]
    profile = "default"
    if rest and rest[0] == "--profile":
        if len(rest) < 2:
            return None
        profile = rest[1]
        rest = rest[2:]
    if len(rest) < 2:
        return None
    repo = rest[0]
    task = " ".join(rest[1:])
    return host, profile, repo, task


def build_worker_and_router(
    wcfg: WorkerConfig, sink: QueenSink, bk: Bookkeeping, registry: Registry,
) -> tuple[QueenRouter, Supervisor]:
    """Wire one queen router + one worker over the in-memory transport (Plan 1)."""
    worker_sink = InMemoryEventSink(sink, wcfg.host, wcfg.profile)
    supervisor = Supervisor(wcfg, registry, worker_sink)
    router = QueenRouter(bk)
    router.register(wcfg.host, wcfg.profile, supervisor)
    return router, supervisor


def build_dispatcher(router: QueenRouter, config: QueenConfig) -> Dispatcher:
    dp = Dispatcher()

    async def owner_mw(
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        for attr in ("message", "edited_message", "callback_query"):
            sub = getattr(event, attr, None)
            user = getattr(sub, "from_user", None) if sub else None
            if user is not None:
                return await handler(event, data) if is_owner(user.id, config.owner_id) else None
        return None

    dp.update.outer_middleware(owner_mw)

    def owner_only(message: Message) -> bool:
        uid = message.from_user.id if message.from_user else None
        return is_owner(uid, config.owner_id)

    @dp.message(Command("spawn"), F.func(owner_only))
    async def _spawn(message: Message, command: CommandObject):
        parsed = parse_spawn(command.args or "")
        if parsed is None:
            await message.answer("Usage: /spawn <host> [--profile <p>] <repo> <task>",
                                 parse_mode=None)
            return
        host, profile, repo, task = parsed
        try:
            await router.cmd_spawn(host, profile, repo, task)
        except UnknownWorker:
            await message.answer(f"No worker for {host}/{profile}", parse_mode=None)
            return
        except CapacityError as exc:
            await message.answer(f"Rejected: {exc}", parse_mode=None)
            return
        await message.answer(f"Spawned on {host}/{profile}", parse_mode=None)

    @dp.message(Command("ls"), F.func(owner_only))
    async def _ls(message: Message):
        await message.answer(router.format_ls())

    @dp.message(Command("kill"), F.func(owner_only))
    async def _kill(message: Message, command: CommandObject):
        if not command.args or not command.args.strip().isdigit():
            await message.answer("Usage: /kill <ref>", parse_mode=None)
            return
        ok = await router.cmd_kill(int(command.args.strip()))
        await message.answer("Killed" if ok else "No such task", parse_mode=None)

    @dp.message(Command("panic"), F.func(owner_only))
    async def _panic(message: Message):
        n = await router.cmd_panic()
        await message.answer(f"Panicked {n} workers", parse_mode=None)

    return dp


async def main() -> None:
    qcfg = load_queen_config(os.environ)
    wcfg = load_worker_config(os.environ)
    bot = build_bot(qcfg)
    gateway = Gateway(bot, qcfg)
    bk = Bookkeeping.open(qcfg.bookkeeping_db)
    sink = QueenSink(gateway, bk)
    registry = Registry.open(wcfg.db_path)
    router, _ = build_worker_and_router(wcfg, sink, bk, registry)
    dp = build_dispatcher(router, qcfg)
    await dp.start_polling(bot)


def run() -> None:
    asyncio.run(main())
