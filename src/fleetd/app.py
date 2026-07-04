from __future__ import annotations

import asyncio
import os

from aiogram import Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from fleetd.config import Config, load_config
from fleetd.db import Registry, Task
from fleetd.formatting import escape_md
from fleetd.supervisor import Supervisor
from fleetd.telegram_gw import Gateway, build_bot, is_owner


def format_ls(tasks: list[Task]) -> str:
    if not tasks:
        return "No active agents\\."
    lines = [f"`{t.id}` {escape_md(t.repo)} — {escape_md(t.status)}" for t in tasks]
    return "\n".join(lines)


_CARRIERS = ("message", "edited_message", "channel_post", "edited_channel_post",
             "callback_query", "inline_query", "my_chat_member", "chat_member")


def build_owner_middleware(config: Config):
    async def middleware(handler, event, data):
        user = None
        for attr in _CARRIERS:
            sub = getattr(event, attr, None)
            if sub is not None and getattr(sub, "from_user", None) is not None:
                user = sub.from_user
                break
        if user is None or not is_owner(user.id, config.owner_id):
            return None  # drop non-owner / userless updates before routing
        return await handler(event, data)
    return middleware


def build_dispatcher(supervisor: Supervisor, config: Config) -> Dispatcher:
    dp = Dispatcher()
    dp.update.outer_middleware(build_owner_middleware(config))

    def owner_only(message: Message) -> bool:
        uid = message.from_user.id if message.from_user else None
        return is_owner(uid, config.owner_id)

    @dp.message(Command("spawn"), F.func(owner_only))
    async def _spawn(message: Message, command: CommandObject):
        args = (command.args or "").split(maxsplit=1)
        if len(args) < 2:
            await message.answer("Usage: /spawn <repo> <task>", parse_mode=None)
            return
        repo, task_text = args[0], args[1]
        try:
            tid = await supervisor.spawn(repo, task_text)
        except Exception as exc:
            await message.answer(f"Spawn failed: {exc}", parse_mode=None)
            return
        await message.answer(f"Spawned task `{tid}` in {escape_md(repo)}")

    @dp.message(Command("ls"), F.func(owner_only))
    async def _ls(message: Message):
        await message.answer(format_ls(supervisor.list_active()))

    @dp.message(Command("kill"), F.func(owner_only))
    async def _kill(message: Message, command: CommandObject):
        if not command.args or not command.args.strip().isdigit():
            await message.answer("Usage: /kill <id>", parse_mode=None)
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
