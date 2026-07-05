from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, TelegramObject

from skep.config import QueenConfig, WorkerConfig, load_queen_config, load_worker_config
from skep.db import Registry
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.mailbox import Mailbox, MailboxService, SendResult
from skep.queen.router import QueenRouter, UnknownWorker
from skep.queen.telegram_sink import QueenSink
from skep.supervisor import CapacityError, Supervisor
from skep.telegram_gw import Gateway, build_bot, is_owner
from skep.transport import InMemoryEventSink

_REPLY_ID_RE = re.compile(r"reply id:\s*(\d+)")


def _extract_reply_id(text: str) -> int | None:
    """Extract the mailbox id from a CEO-delivery's 'reply id: <n>' footer.

    Uses the LAST match: deliver_ceo always appends this footer AFTER the
    lower-trust, sender-controlled subject/body, so an injected earlier
    'reply id: N' in message content can never win (prevents reply misrouting).
    """
    matches = _REPLY_ID_RE.findall(text)
    return int(matches[-1]) if matches else None


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


async def handle_ceo_reply(
    service: MailboxService,
    in_reply_to: int | None,
    to: str,
    subject: str,
    body: str,
) -> SendResult:
    """Forward an owner-authored Telegram reply into the mailbox as `ceo`."""
    return await service.handle_send(
        sender="ceo", to=to, subject=subject, body=body,
        in_reply_to=in_reply_to)


def build_worker_and_router(
    wcfg: WorkerConfig, sink: QueenSink, bk: Bookkeeping, registry: Registry,
) -> tuple[QueenRouter, Supervisor]:
    """Wire one queen router + one worker over the in-memory transport (Plan 1)."""
    worker_sink = InMemoryEventSink(sink, wcfg.host, wcfg.profile)
    supervisor = Supervisor(wcfg, registry, worker_sink)
    router = QueenRouter(bk)
    router.register(wcfg.host, wcfg.profile, supervisor)
    return router, supervisor


def build_dispatcher(
    router: QueenRouter,
    config: QueenConfig,
    mailbox_service: MailboxService | None = None,
    mailbox: Mailbox | None = None,
) -> Dispatcher:
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

    if mailbox_service is not None and mailbox is not None:
        @dp.message(F.reply_to_message, F.func(owner_only))
        async def _ceo_reply(message: Message):
            reply = message.reply_to_message
            source_text = (reply.text or reply.caption or "") if reply else ""
            reply_id = _extract_reply_id(source_text)
            if reply_id is None:
                # Not a reply to a mailbox delivery -- nothing for us to do.
                return
            original = mailbox.get(reply_id)
            if original is None:
                await message.answer(
                    f"Can't find mailbox message {reply_id}", parse_mode=None)
                return
            body = message.text or message.caption or ""
            res = await handle_ceo_reply(
                mailbox_service,
                in_reply_to=reply_id,
                to=original.sender,
                subject=f"Re: {original.subject}",
                body=body,
            )
            if res.ok:
                await message.answer("Sent", parse_mode=None)
            else:
                await message.answer(f"Failed: {res.error}", parse_mode=None)

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
