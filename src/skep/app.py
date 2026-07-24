from __future__ import annotations

import asyncio
import contextlib
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, TelegramObject

from skep.config import QueenConfig, WorkerConfig, load_queen_config, load_worker_config
from skep.db import Registry
from skep.queen.assembly import (
    _ceo_retry_loop,
    _mailbox_db_path,
    _park_sweep_loop,
    build_mailbox_service,
)
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.mailbox import Mailbox, MailboxService, SendResult, agent_sender
from skep.queen.router import QueenRouter, UnknownWorker
from skep.queen.telegram_sink import QueenSink
from skep.supervisor import CapacityError, Supervisor
from skep.telegram_gw import Gateway, build_bot, is_owner
from skep.transport import (
    InMemoryEventSink,
    InMemoryMailboxClient,
    SwitchableMailboxClient,
)
from skep.worker.roots import RootError

_REPLY_ID_RE = re.compile(r"reply id:\s*(\d+)")


def _extract_reply_id(text: str) -> int | None:
    """Extract the mailbox id from a CEO-delivery's 'reply id: <n>' footer.

    Uses the LAST match: deliver_ceo always appends this footer AFTER the
    lower-trust, sender-controlled subject/body, so an injected earlier
    'reply id: N' in message content can never win (prevents reply misrouting).
    """
    matches = _REPLY_ID_RE.findall(text)
    return int(matches[-1]) if matches else None


def parse_spawn(args: str) -> tuple[str, str, str, bool, str] | None:
    """Parse `<host> [--profile <p>] <repo> [--watch] <task...>`.

    Returns (host, profile, repo, watch, task). `--watch` adds the repo's main
    checkout as a read-only second root, so the agent can see uncommitted work
    in the operator's tree while working in its own worktree.
    """
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
    rest = rest[1:]
    watch = False
    if rest and rest[0] == "--watch":
        watch = True
        rest = rest[1:]
    if not rest:
        return None
    task = " ".join(rest)
    return host, profile, repo, watch, task


def watch_roots(repo: str) -> list[dict[str, str]]:
    """The canonical two-root workspace: own worktree + primary checkout, ro."""
    return [
        {"name": repo, "mode": "new", "access": "rw"},
        {"name": repo, "mode": "primary", "access": "ro"},
    ]


async def handle_ceo_reply(
    service: MailboxService,
    in_reply_to: int | None,
    to: str,
    subject: str,
    body: str,
) -> SendResult:
    """Forward an owner-authored Telegram reply into the mailbox as `ceo`."""
    return await service.handle_send(
        sender="ceo", to=to, subject=subject, body=body, in_reply_to=in_reply_to
    )


def build_worker_and_router(
    wcfg: WorkerConfig,
    sink: QueenSink,
    bk: Bookkeeping,
    registry: Registry,
    mailbox_service: MailboxService | None = None,
) -> tuple[QueenRouter, Supervisor]:
    """Wire one queen router + one worker over the in-memory transport (Plan 1).

    When a `mailbox_service` is given, the Supervisor's mailbox client is
    pointed at an in-process target so agent sends are delivered directly
    (single-process mode); without it the switch has no target and sends raise
    MailboxUnavailable (the WS path attaches its own target at connect time).
    """
    worker_sink = InMemoryEventSink(sink, wcfg.host, wcfg.profile)
    mailbox_switch = SwitchableMailboxClient()
    if mailbox_service is not None:

        def _sender_for_tid(tid: int) -> str:
            return agent_sender(bk, wcfg.host, wcfg.profile, tid)

        mailbox_switch.set_target(
            InMemoryMailboxClient(mailbox_service, _sender_for_tid)
        )
    supervisor = Supervisor(wcfg, registry, worker_sink, mailbox_client=mailbox_switch)
    router = QueenRouter(bk)
    router.register(wcfg.host, wcfg.profile, supervisor)
    # The in-process worker has no connection to lose, so it is online from the
    # moment it is wired. Only the WS path marks presence on connect, so without
    # this the sole worker reads as detached forever: `/ls` labels it
    # "(detached)" and the park sweep, which skips offline workers, would never
    # auto-resume anything on this path.
    router.mark_online(wcfg.host, wcfg.profile)
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
                return (
                    await handler(event, data)
                    if is_owner(user.id, config.owner_id)
                    else None
                )
        return None

    dp.update.outer_middleware(owner_mw)

    def owner_only(message: Message) -> bool:
        uid = message.from_user.id if message.from_user else None
        return is_owner(uid, config.owner_id)

    @dp.message(Command("spawn"), F.func(owner_only))
    async def _spawn(message: Message, command: CommandObject) -> None:
        parsed = parse_spawn(command.args or "")
        if parsed is None:
            await message.answer(
                "Usage: /spawn <host> [--profile <p>] <repo> [--watch] <task>",
                parse_mode=None,
            )
            return
        host, profile, repo, watch, task = parsed
        try:
            await router.cmd_spawn(
                host, profile, repo, task, roots=watch_roots(repo) if watch else None
            )
        except UnknownWorker:
            await message.answer(f"No worker for {host}/{profile}", parse_mode=None)
            return
        except CapacityError as exc:
            await message.answer(f"Rejected: {exc}", parse_mode=None)
            return
        except RootError as exc:
            await message.answer(f"Rejected: {exc}", parse_mode=None)
            return
        await message.answer(f"Spawned on {host}/{profile}", parse_mode=None)

    @dp.message(Command("ls"), F.func(owner_only))
    async def _ls(message: Message) -> None:
        await message.answer(router.format_ls())

    @dp.message(Command("kill"), F.func(owner_only))
    async def _kill(message: Message, command: CommandObject) -> None:
        if not command.args or not command.args.strip().isdigit():
            await message.answer("Usage: /kill <ref>", parse_mode=None)
            return
        # Killing a *parked* session ends it with no worker `done` event to
        # follow, so the mailbox teardown that on_done normally does has to be
        # handed down here. This handler is shared by both runtime shapes.
        ok = await router.cmd_kill(
            int(command.args.strip()),
            on_session_ended=(
                mailbox_service.handle_recipient_gone
                if mailbox_service is not None
                else None
            ),
        )
        await message.answer("Killed" if ok else "No such task", parse_mode=None)

    @dp.message(Command("resume"), F.func(owner_only))
    async def _resume(message: Message, command: CommandObject) -> None:
        args = (command.args or "").split()
        model: str | None = None
        if "--model" in args:
            i = args.index("--model")
            model = args[i + 1] if i + 1 < len(args) else None
            args = args[:i] + args[i + 2 :]
        if len(args) != 1 or not args[0].isdigit():
            await message.answer(
                "Usage: /resume <ref> [--model <m>]", parse_mode=None
            )
            return
        try:
            ok = await router.cmd_resume(int(args[0]), model)
        except (CapacityError, ValueError) as exc:
            # Single-process: the router's handler IS a Supervisor, so "at
            # capacity", "no such session", "no resume_token" and "already has a
            # live invocation" raise straight through this handler and the owner
            # would get silence. Split-queen reports the same rejections out of
            # band as `spawn_rejected`. Mirrors _spawn.
            await message.answer(f"Rejected: {exc}", parse_mode=None)
            return
        await message.answer(
            f"Resuming ref {args[0]}" if ok else "No such session / already running",
            parse_mode=None,
        )

    @dp.message(Command("panic"), F.func(owner_only))
    async def _panic(message: Message) -> None:
        n = await router.cmd_panic()
        await message.answer(f"Panicked {n} workers", parse_mode=None)

    if mailbox_service is not None and mailbox is not None:

        @dp.message(F.reply_to_message, F.func(owner_only))
        async def _ceo_reply(message: Message) -> None:
            reply = message.reply_to_message
            source_text = (reply.text or reply.caption or "") if reply else ""
            reply_id = _extract_reply_id(source_text)
            if reply_id is None:
                # Not a reply to a mailbox delivery -- nothing for us to do.
                return
            original = mailbox.get(reply_id)
            if original is None:
                await message.answer(
                    f"Can't find mailbox message {reply_id}", parse_mode=None
                )
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
    mailbox = Mailbox.open(_mailbox_db_path(qcfg.bookkeeping_db))
    mailbox_service = build_mailbox_service(qcfg, gateway, bk, mailbox)
    sink = QueenSink(
        gateway,
        bk,
        mailbox_service=mailbox_service,
        park_default_backoff=qcfg.park_default_backoff,
    )
    registry = Registry.open(wcfg.db_path)
    router, _ = build_worker_and_router(
        wcfg, sink, bk, registry, mailbox_service=mailbox_service
    )
    dp = build_dispatcher(
        router, qcfg, mailbox_service=mailbox_service, mailbox=mailbox
    )

    # No aiohttp app on this path (aiogram polling), so the background loops the
    # queen daemon ties to app.cleanup_ctx run as plain tasks tied to the
    # polling loop's lifetime instead.
    background: list[asyncio.Task[None]] = []
    if qcfg.mailbox_ceo_retry_interval > 0:
        background.append(
            asyncio.create_task(
                _ceo_retry_loop(mailbox_service, qcfg.mailbox_ceo_retry_interval)
            )
        )
    if qcfg.park_sweep_interval > 0:
        background.append(
            asyncio.create_task(
                _park_sweep_loop(bk, router, qcfg.park_sweep_interval)
            )
        )
    try:
        await dp.start_polling(bot)
    finally:
        for task in background:
            task.cancel()
        for task in background:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def run() -> None:
    asyncio.run(main())
