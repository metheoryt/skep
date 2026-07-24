from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from skep.formatting import escape_md
from skep.queen.bookkeeping import Bookkeeping
from skep.transport import CommandHandler


class UnknownWorker(Exception):
    """Raised when a command targets a (host, profile) with no registered worker."""


class QueenRouter:
    def __init__(
        self, bookkeeping: Bookkeeping, *, now: Callable[[], float] = time.monotonic
    ) -> None:
        self._bk = bookkeeping
        self._workers: dict[tuple[str, str], CommandHandler] = {}
        self._online: set[tuple[str, str]] = set()
        self._last_seen: dict[tuple[str, str], float] = {}
        self._now = now

    def register(self, host: str, profile: str, handler: CommandHandler) -> None:
        self._workers[(host, profile)] = handler

    def unregister(self, host: str, profile: str) -> None:
        self._workers.pop((host, profile), None)

    def detach_if_current(
        self, host: str, profile: str, handler: CommandHandler
    ) -> bool:
        """Unregister+mark-offline only if `handler` is still the live one.

        Guards against a reconnect race: an old connection's cleanup must
        not clobber a newer connection that already replaced it in the
        registry (see ws_transport.QueenWsServer._handle).
        """
        if self._workers.get((host, profile)) is not handler:
            return False
        self.mark_offline(host, profile)
        self._workers.pop((host, profile), None)
        return True

    def mark_online(self, host: str, profile: str) -> None:
        self._online.add((host, profile))
        self._last_seen[(host, profile)] = self._now()

    def mark_offline(self, host: str, profile: str) -> None:
        self._online.discard((host, profile))

    def touch(self, host: str, profile: str) -> None:
        self._last_seen[(host, profile)] = self._now()

    def is_online(self, host: str, profile: str) -> bool:
        return (host, profile) in self._online

    def last_seen(self, host: str, profile: str) -> float | None:
        return self._last_seen.get((host, profile))

    async def cmd_spawn(
        self,
        host: str,
        profile: str,
        repo: str,
        task: str,
        roots: list[dict[str, Any]] | None = None,
    ) -> None:
        handler = self._workers.get((host, profile))
        if handler is None:
            raise UnknownWorker(f"{host}/{profile}")
        await handler.spawn(repo, task, roots)

    async def cmd_kill(
        self,
        ref: int,
        *,
        on_session_ended: Callable[[int], Awaitable[None]] | None = None,
    ) -> bool:
        """`on_session_ended` runs only when this call itself ends a session --
        i.e. the parked branch below, which has no worker `done` event to
        follow. A live invocation's kill still ends through the worker, and
        QueenSink.on_done does the teardown there as it always has. Passed per
        call rather than held as a dependency so the single /kill handler in
        build_dispatcher (shared by BOTH runtime shapes) stays the only wiring
        site -- one more constructor argument would mean two build sites to keep
        in sync, which is exactly how park_default_backoff came to be dropped on
        the single-process path."""
        entry = self._bk.get(ref)
        if entry is None:
            return False
        if entry.status == "parked":
            # A parked session has NO process: `local_id` names the invocation
            # that already died on the usage limit. Sending a kill frame is at
            # best a no-op and at worst a lie -- RemoteWorker.kill returns True
            # unconditionally, so the split queen answered "Killed" while the
            # row stayed parked, stayed in _ACTIVE, stayed in parked_due, and
            # the sweep auto-resumed it at the next reset. End the session here
            # instead: 'killed' is outside both _ACTIVE and parked_due, so the
            # sweep stops seeing it and the answer becomes true. This is also
            # the only way to cancel a parked session at all.
            self._bk.set_status(ref, "killed")
            if on_session_ended is not None:
                await on_session_ended(ref)
            return True
        handler = self._workers.get((entry.host, entry.profile))
        if handler is None:
            return False
        await handler.kill(entry.local_id)
        return True

    async def cmd_resume(
        self, ref: int, model: str | None = None, origin: str | None = None
    ) -> bool:
        """`origin` names who asked: "sweep" for the auto-resume sweep, None for
        a human's /resume. It rides the dispatch so a rejection can be traced
        back to its caller (see wire.resume_msg)."""
        entry = self._bk.get(ref)
        if entry is None:
            return False
        # Skip a session that is already running. This is a cheap filter, NOT a
        # mutual-exclusion guard: nothing here flips the status. 'running' is set
        # only when the worker's task_started event round-trips back into
        # Bookkeeping.rebind_invocation, so between this read and that write
        # there is a window as wide as a process spawn in which a second caller
        # reads the same stale status and dispatches a second resume.
        # Deduplication belongs in Supervisor.resume, which alone knows whether a
        # session already has a live invocation. See Task 9 in the A3 plan.
        if entry.status == "running":
            return False
        handler = self._workers.get((entry.host, entry.profile))
        if handler is None:
            return False
        await handler.resume(entry.session_local_id, model=model, origin=origin)
        return True

    async def cmd_panic(self) -> int:
        for handler in list(self._workers.values()):
            await handler.panic()
        return len(self._workers)

    def format_ls(self) -> str:
        entries = self._bk.list_active()
        if not entries:
            return "No active agents\\."
        lines = []
        for e in entries:
            marker = "" if self.is_online(e.host, e.profile) else " \\(detached\\)"
            lines.append(
                f"`{e.ref}` {escape_md(e.host)}/{escape_md(e.profile)} "
                f"{escape_md(e.repo)} — {escape_md(e.status)}{marker}"
            )
        return "\n".join(lines)
