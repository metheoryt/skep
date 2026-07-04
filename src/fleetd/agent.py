from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import AsyncIterator

from fleetd.stream import Event, parse_event


def create_worktree(repo_path: Path, worktree_path: Path, branch: str) -> None:
    import subprocess

    subprocess.run(
        ["git", "-C", str(repo_path), "worktree", "add", "-b", branch,
         str(worktree_path)],
        check=True,
        capture_output=True,
    )


class AgentProcess:
    def __init__(self, task_text: str, cwd: Path, claude_bin: str):
        self._task_text = task_text
        self._cwd = cwd
        self._claude_bin = claude_bin
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def _argv(self) -> list[str]:
        # claude_bin may be a multi-token command (e.g. "python fake_claude.py").
        base = shlex.split(self._claude_bin)
        return [
            *base, "-p", self._task_text,
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
        ]

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv(),
            cwd=str(self._cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def events(self) -> AsyncIterator[Event]:
        assert self._proc is not None and self._proc.stdout is not None
        async for raw in self._proc.stdout:
            ev = parse_event(raw.decode(errors="replace"))
            if ev is not None:
                yield ev
        await self._proc.wait()

    async def kill(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
