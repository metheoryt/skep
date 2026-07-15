from __future__ import annotations

import asyncio
import json
import os
import shlex
from collections.abc import AsyncIterator
from pathlib import Path

from skep.stream import Event, parse_event

_CORE_ENV_KEYS = (
    "PATH", "HOME", "USER", "LOGNAME", "TERM", "LANG", "TZ", "SHELL",
)
# Kept only if present. Proxy vars matter because dropping HTTPS_PROXY on a
# proxied host kills the API and dropping NO_PROXY=127.0.0.1 sends the loopback
# shim call through the proxy. Spec 5.1 anticipates this set growing via spike (a).
_OPTIONAL_ENV_KEYS = (
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NIX_SSL_CERT_FILE", "LOCALE_ARCHIVE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
)


def _agent_env(config_dir: str | None, *,
               passthrough: tuple[str, ...] = ()) -> dict[str, str]:
    """Build a default-drop allowlisted environment for a spawned agent.

    Scrubs the worker's own environment -- critically the entire SKEP_*
    namespace (SKEP_SHARED_SECRET, SKEP_DB), CLAUDE_CODE_*/CLAUDECODE session
    markers, and ANTHROPIC_* -- so a compromised agent cannot read them from
    its OWN environ. This does NOT stop a same-UID sibling reading the worker's
    /proc/<pid>/environ; that cross-process pivot closes only with L0.2
    Increment 2's PID namespace. Surface reduction, not full containment.

    CLAUDE_CONFIG_DIR is set explicitly from config_dir and never inherited.
    """
    src = os.environ
    env: dict[str, str] = {}
    for key in _CORE_ENV_KEYS:
        if key in src:
            env[key] = src[key]
    for key, val in src.items():
        if key.startswith("LC_"):
            env[key] = val
    for key in _OPTIONAL_ENV_KEYS:
        if key in src:
            env[key] = src[key]
    for key in passthrough:
        if key in src:
            env[key] = src[key]
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    return env


def create_worktree(repo_path: Path, worktree_path: Path, branch: str) -> None:
    import subprocess

    subprocess.run(
        [
            "git",
            "-C",
            str(repo_path),
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree_path),
        ],
        check=True,
        capture_output=True,
    )


class AgentProcess:
    def __init__(
        self,
        task_text: str,
        cwd: Path,
        claude_bin: str,
        config_dir: str | None = None,
        mcp_servers: dict[str, dict] | None = None,
        allowed_tools: list[str] | None = None,
        append_system_prompt: str | None = None,
        add_dirs: list[Path] | None = None,
        model: str | None = None,
        resume_token: str | None = None,
        env_passthrough: tuple[str, ...] = (),
    ) -> None:
        self._task_text = task_text
        self._cwd = cwd
        self._claude_bin = claude_bin
        self._config_dir = config_dir
        self._mcp_servers = mcp_servers
        self._allowed_tools = allowed_tools
        self._append_system_prompt = append_system_prompt
        self._add_dirs = add_dirs
        self._model = model
        self._resume_token = resume_token
        self._env_passthrough = env_passthrough
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
            *base,
            "-p",
            self._task_text,
            "--output-format",
            "stream-json",
            # --input-format stream-json is Phase 2 (soft-steer); Phase 1 is one-shot via -p
            "--verbose",
        ]
        if self._append_system_prompt is not None:
            argv += ["--append-system-prompt", self._append_system_prompt]
        if self._mcp_servers:
            argv += ["--mcp-config", json.dumps({"mcpServers": self._mcp_servers})]
        if self._allowed_tools:
            # Comma-joined single token: the form verified by the §2.4 probe.
            # The enumeration must be COMPLETE -- skep never relies on a host
            # profile's allowlist surviving this flag (spec §2.2).
            argv += ["--allowedTools", ",".join(self._allowed_tools)]
        if self._add_dirs:
            for d in self._add_dirs:
                argv += ["--add-dir", str(d)]
        if self._model is not None:
            argv += ["--model", self._model]
        if self._resume_token is not None:
            argv += ["--resume", self._resume_token]
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
            env=_agent_env(self._config_dir, passthrough=self._env_passthrough),
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
            except TimeoutError:
                self._proc.kill()
                await self._proc.wait()
