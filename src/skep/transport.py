from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from skep.queen.mailbox import Message, MailboxService


@runtime_checkable
class EventSink(Protocol):
    """Worker -> queen. The worker's Supervisor calls these; identity is implicit."""
    async def task_started(self, local_id: int, repo: str, title: str) -> None: ...
    async def activity(self, local_id: int, line: str) -> None: ...
    async def milestone(self, local_id: int, text: str) -> None: ...
    async def done(self, local_id: int, status: str, summary: str) -> None: ...


@runtime_checkable
class CommandHandler(Protocol):
    """Queen -> worker. The worker (Supervisor) implements these.

    Return values (new task id / killed? / count) are for local callers and
    tests; the queen router ignores them. Typed truthfully so the concrete
    Supervisor conforms to the protocol.
    """
    async def spawn(self, repo: str, task: str) -> int: ...
    async def kill(self, task_id: int) -> bool: ...
    async def panic(self) -> int: ...


@runtime_checkable
class QueenInbox(Protocol):
    """The queen's receiving side. Each call carries the sender (host, profile)."""
    async def on_task_started(self, host: str, profile: str, local_id: int,
                              repo: str, title: str) -> None: ...
    async def on_activity(self, host: str, profile: str, local_id: int,
                          line: str) -> None: ...
    async def on_milestone(self, host: str, profile: str, local_id: int,
                           text: str) -> None: ...
    async def on_done(self, host: str, profile: str, local_id: int,
                      status: str, summary: str) -> None: ...
    async def on_spawn_rejected(self, host: str, profile: str,
                                reason: str) -> None: ...


class InMemoryEventSink:
    """An EventSink bound to one worker's (host, profile) that forwards to a QueenInbox.

    Plan 2 adds a WebSocket EventSink implementing the same interface.
    """

    def __init__(self, inbox: QueenInbox, host: str, profile: str):
        self._inbox = inbox
        self._host = host
        self._profile = profile

    async def task_started(self, local_id: int, repo: str, title: str) -> None:
        await self._inbox.on_task_started(self._host, self._profile, local_id, repo, title)

    async def activity(self, local_id: int, line: str) -> None:
        await self._inbox.on_activity(self._host, self._profile, local_id, line)

    async def milestone(self, local_id: int, text: str) -> None:
        await self._inbox.on_milestone(self._host, self._profile, local_id, text)

    async def done(self, local_id: int, status: str, summary: str) -> None:
        await self._inbox.on_done(self._host, self._profile, local_id, status, summary)


class SwitchableEventSink:
    """A stable EventSink the Supervisor holds; its target is swapped per WS
    connection. When target is None (worker detached) events are dropped —
    agents keep running, only reporting pauses (design §6.4)."""

    def __init__(self) -> None:
        self.target: EventSink | None = None

    async def task_started(self, local_id: int, repo: str, title: str) -> None:
        if self.target is not None:
            await self.target.task_started(local_id, repo, title)

    async def activity(self, local_id: int, line: str) -> None:
        if self.target is not None:
            await self.target.activity(local_id, line)

    async def milestone(self, local_id: int, text: str) -> None:
        if self.target is not None:
            await self.target.milestone(local_id, text)

    async def done(self, local_id: int, status: str, summary: str) -> None:
        if self.target is not None:
            await self.target.done(local_id, status, summary)


class MailboxUnavailable(Exception):
    """Raised when no mailbox transport target is attached."""


@dataclass
class SendReply:
    ok: bool
    message_id: int | None
    error: str | None
    status: str


class MailboxClient(Protocol):
    """Worker -> queen mailbox handle. Task 11 adds a WS-backed implementation."""
    async def send(
        self, tid: int, to: str, subject: str, body: str,
        in_reply_to: int | None,
    ) -> SendReply: ...

    async def read(self, tid: int) -> list[dict[str, Any]]: ...


def _message_to_dict(m: Message) -> dict[str, Any]:
    return {
        "id": m.id,
        "sender": m.sender,
        "subject": m.subject,
        "body": m.body,
        "created_at": m.created_at,
        "in_reply_to": m.in_reply_to,
    }


class InMemoryMailboxClient:
    """Direct in-process MailboxClient over a MailboxService (tests/single-host)."""

    def __init__(
        self, service: MailboxService, sender_for_tid: Callable[[int], str],
    ) -> None:
        self._svc = service
        self._sender_for_tid = sender_for_tid

    async def send(
        self, tid: int, to: str, subject: str, body: str,
        in_reply_to: int | None,
    ) -> SendReply:
        sender = self._sender_for_tid(tid)
        res = await self._svc.handle_send(
            sender=sender, to=to, subject=subject, body=body,
            in_reply_to=in_reply_to)
        return SendReply(res.ok, res.message_id, res.error, res.status)

    async def read(self, tid: int) -> list[dict[str, Any]]:
        recipient = self._sender_for_tid(tid)
        msgs = await self._svc.handle_read(recipient)
        return [_message_to_dict(m) for m in msgs]


class SwitchableMailboxClient:
    """Target-swappable MailboxClient (mirrors SwitchableEventSink)."""

    def __init__(self) -> None:
        self._target: MailboxClient | None = None

    def set_target(self, target: MailboxClient | None) -> None:
        self._target = target

    async def send(
        self, tid: int, to: str, subject: str, body: str,
        in_reply_to: int | None,
    ) -> SendReply:
        if self._target is None:
            raise MailboxUnavailable("no mailbox transport attached")
        return await self._target.send(tid, to, subject, body, in_reply_to)

    async def read(self, tid: int) -> list[dict[str, Any]]:
        if self._target is None:
            raise MailboxUnavailable("no mailbox transport attached")
        return await self._target.read(tid)
