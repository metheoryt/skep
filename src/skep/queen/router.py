from __future__ import annotations

import time
from collections.abc import Callable
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

    async def cmd_kill(self, ref: int) -> bool:
        entry = self._bk.get(ref)
        if entry is None:
            return False
        handler = self._workers.get((entry.host, entry.profile))
        if handler is None:
            return False
        await handler.kill(entry.local_id)
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
