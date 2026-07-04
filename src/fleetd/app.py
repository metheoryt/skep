from __future__ import annotations

import asyncio
import os

from aiogram import Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from fleetd.config import Config, load_config
from fleetd.db import Registry, Task
from fleetd.supervisor import Supervisor
from fleetd.telegram_gw import Gateway, build_bot, is_owner


def format_ls(tasks: list[Task]) -> str:
    if not tasks:
        return "No active agents\\."
    lines = [f"`{t.id}` {t.repo} — {t.status}" for t in tasks]
    return "\n".join(lines)


def build_dispatcher(supervisor: Supervisor, config: Config) -> Dispatcher:
    dp = Dispatcher()

    def owner_only(message: Message) -> bool:
        uid = message.from_user.id if message.from_user else None
        return is_owner(uid, config.owner_id)

    @dp.message(Command("spawn"), F.func(owner_only))
    async def _spawn(message: Message, command: CommandObject):
        args = (command.args or "").split(maxsplit=1)
        if len(args) < 2:
            await message.answer("Usage: /spawn <repo> <task>")
            return
        repo, task_text = args[0], args[1]
        tid = await supervisor.spawn(repo, task_text)
        await message.answer(f"Spawned task `{tid}` in {repo}")

    @dp.message(Command("ls"), F.func(owner_only))
    async def _ls(message: Message):
        await message.answer(format_ls(supervisor.list_active()))

    @dp.message(Command("kill"), F.func(owner_only))
    async def _kill(message: Message, command: CommandObject):
        if not command.args or not command.args.strip().isdigit():
            await message.answer("Usage: /kill <id>")
            return
        ok = await supervisor.kill(int(command.args.strip()))
        await message.answer("Killed" if ok else "No such active task")

    @dp.message(Command("panic"), F.func(owner_only))
    async def _panic(message: Message):
        n = await supervisor.panic()
        await message.answer(f"Killed {n} agents")

    return dp


async def main() -> None:
    config = load_config(os.environ)
    registry = Registry.open(os.environ.get("FLEETD_DB", "fleetd.sqlite"))
    bot = build_bot(config)
    gateway = Gateway(bot, config)
    supervisor = Supervisor(config, registry, gateway)
    dp = build_dispatcher(supervisor, config)
    await dp.start_polling(bot)


def run() -> None:
    asyncio.run(main())
