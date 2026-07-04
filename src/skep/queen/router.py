from __future__ import annotations

from skep.formatting import escape_md
from skep.queen.bookkeeping import Bookkeeping
from skep.transport import CommandHandler


class UnknownWorker(Exception):
    """Raised when a command targets a (host, profile) with no registered worker."""


class QueenRouter:
    def __init__(self, bookkeeping: Bookkeeping):
        self._bk = bookkeeping
        self._workers: dict[tuple[str, str], CommandHandler] = {}

    def register(self, host: str, profile: str, handler: CommandHandler) -> None:
        self._workers[(host, profile)] = handler

    async def cmd_spawn(self, host: str, profile: str, repo: str, task: str) -> None:
        handler = self._workers.get((host, profile))
        if handler is None:
            raise UnknownWorker(f"{host}/{profile}")
        await handler.spawn(repo, task)

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
        lines = [
            f"`{e.ref}` {escape_md(e.host)}/{escape_md(e.profile)} "
            f"{escape_md(e.repo)} — {escape_md(e.status)}"
            for e in entries
        ]
        return "\n".join(lines)
