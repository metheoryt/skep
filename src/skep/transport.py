from __future__ import annotations

from typing import Protocol, runtime_checkable


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
