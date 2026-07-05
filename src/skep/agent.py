from __future__ import annotations

import asyncio
import json
import os
import shlex
from pathlib import Path
from typing import AsyncIterator

from skep.stream import Event, parse_event


def _agent_env(config_dir: str | None) -> dict[str, str]:
    env = dict(os.environ)
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    return env


def create_worktree(repo_path: Path, worktree_path: Path, branch: str) -> None:
    import subprocess

    subprocess.run(
        ["git", "-C", str(repo_path), "worktree", "add", "-b", branch,
         str(worktree_path)],
        check=True,
        capture_output=True,
    )


class AgentProcess:
    def __init__(self, task_text: str, cwd: Path, claude_bin: str,
                 config_dir: str | None = None,
                 mcp_url: str | None = None,
                 mcp_token: str | None = None):
        self._task_text = task_text
        self._cwd = cwd
        self._claude_bin = claude_bin
        self._config_dir = config_dir
        self._mcp_url = mcp_url
        self._mcp_token = mcp_token
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr: bytes = b""
        self._stderr_task: asyncio.Task | None = None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def _argv(self) -> list[str]:
        # claude_bin may be a multi-token command (e.g. "python fake_claude.py").
        base = shlex.split(self._claude_bin)
        argv = [
            *base, "-p", self._task_text,
            "--output-format", "stream-json",
            # --input-format stream-json is Phase 2 (soft-steer); Phase 1 is one-shot via -p
            "--verbose",
        ]
        if self._mcp_url is not None:
            server: dict[str, object] = {"type": "http", "url": self._mcp_url}
            if self._mcp_token is not None:
                server["headers"] = {
                    "Authorization": f"Bearer {self._mcp_token}"
                }
            mcp_config = {"mcpServers": {"mailbox": server}}
            argv += ["--mcp-config", json.dumps(mcp_config)]
        return argv

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc else None

    @property
    def stderr_text(self) -> str:
        return self._stderr.decode(errors="replace")

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        self._stderr = await self._proc.stderr.read()

    async def start(self) -> None:
        self._stderr = b""
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv(),
            cwd=str(self._cwd),
            env=_agent_env(self._config_dir),
            # Phase 1 writes no stdin; DEVNULL gives immediate EOF, avoiding
            # claude's 3s "no stdin data" stall. Phase 2 (soft-steer) will
            # need PIPE to write follow-up input.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def events(self) -> AsyncIterator[Event]:
        assert self._proc is not None and self._proc.stdout is not None
        async for raw in self._proc.stdout:
            ev = parse_event(raw.decode(errors="replace"))
            if ev is not None:
                yield ev
        await self._proc.wait()
        if self._stderr_task is not None:
            await self._stderr_task

    async def kill(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
