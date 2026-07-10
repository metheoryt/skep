from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from skep.agent import AgentProcess, create_worktree
from skep.config import WorkerConfig
from skep.db import Registry, Task
from skep.formatting import activity_line, milestone_message
from skep.memory import MemoryProbe
from skep.transport import EventSink, MailboxClient
from skep.worker.mcp_shim import MailboxShim
from skep.worker.memory_shim import MEMORY_TOOLS, memory_shim_server

BASE_TOOLS: tuple[str, ...] = ("Bash", "Edit", "Write")
"""The coding baseline (spec §2.3). `Read` is absent: it needs no grant (§2.5).

Enumerated on argv because skep may never assume a host profile's allowlist
survives its own --allowedTools (§2.2).
"""

MAILBOX_TOOLS: tuple[str, ...] = (
    "mcp__mailbox__send_message",
    "mcp__mailbox__read_inbox",
)
"""Exact names, not `mcp__mailbox__*`: the wildcard form was never validated.

`mailbox` here is the --mcp-config MAP KEY. MailboxShim advertises itself as
FastMCP("skep-mailbox"), so these names are only correct if the grant follows
the key rather than the advertised name. Task 8 settles it; until it runs,
treat this constant as unverified.
"""


class CapacityError(Exception):
    """Raised when a worker is at max_concurrent and cannot accept a new task."""


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:24] or "task"


class Supervisor:
    def __init__(
        self,
        config: WorkerConfig,
        registry: Registry,
        sink: EventSink,
        agent_factory: Callable[..., AgentProcess] = AgentProcess,
        worktree_factory: Callable[[Path, Path, str], None] = create_worktree,
        mailbox_client: MailboxClient | None = None,
        shim_factory: Callable[..., MailboxShim] = MailboxShim,
        memory: MemoryProbe | None = None,
    ) -> None:
        self._cfg = config
        self._reg = registry
        self._sink = sink
        self._agent_factory = agent_factory
        self._worktree_factory = worktree_factory
        self._mailbox_client = mailbox_client
        self._shim_factory = shim_factory
        self._memory = memory
        self._agents: dict[int, AgentProcess] = {}
        self._shims: dict[int, MailboxShim] = {}
        self._tasks: set[asyncio.Task] = set()

    def list_active(self) -> list[Task]:
        return self._reg.list_active()

    def _task(self, task_id: int) -> Task:
        task = self._reg.get_task(task_id)
        assert task is not None, f"task {task_id} vanished from registry"
        return task

    async def spawn(self, repo: str, task: str) -> int:
        if len(self._agents) >= self._cfg.max_concurrent:
            raise CapacityError(f"at capacity ({self._cfg.max_concurrent} running)")
        repo_path = self._cfg.repos_root / repo
        tid = self._reg.add_task(repo, task, "", mode="native")
        branch = f"skep/{_slug(task)}-{tid}"
        worktree_path = self._cfg.worktrees_root / f"{repo}-{tid}"
        self._reg.update(tid, worktree_path=str(worktree_path))

        agent: AgentProcess | None = None
        shim: MailboxShim | None = None
        try:
            self._worktree_factory(repo_path, worktree_path, branch)
            self._reg.log_audit(tid, "spawn", f"{repo}: {task}")

            agent_kwargs: dict[str, Any] = dict(
                task_text=task,
                cwd=worktree_path,
                claude_bin=self._cfg.claude_bin,
                config_dir=self._cfg.claude_config_dir,
            )
            mcp_servers: dict[str, dict] = {}
            allowed_tools: list[str] = list(BASE_TOOLS)

            if self._cfg.memory_enabled:
                # Read is a soft dependency: a broken store must never fail a
                # spawn, and must never take down the WRITE channel either
                # (spec §6). `repo_path` is the PARENT repo -- a fact must
                # survive task failure and branch abandonment.
                if self._memory is not None:
                    try:
                        addendum = await self._memory.addendum_for(repo_path)
                    except Exception as exc:
                        self._reg.log_audit(tid, "error", f"memory read failed: {exc}")
                        addendum = None
                    if addendum is not None:
                        agent_kwargs["append_system_prompt"] = addendum
                # The shim is a stdio subprocess of `claude`, not of skep: no
                # start(), no _shims entry, no teardown, no leak on a failed
                # spawn (spec §5.2).
                mcp_servers["memory"] = memory_shim_server(repo_path)
                allowed_tools += MEMORY_TOOLS

            if self._mailbox_client is not None:
                # Per-agent bearer token: the shim enforces it, the agent
                # presents it via --mcp-config. Defeats a passive
                # port-scan-and-connect from another local process; a same-UID
                # sibling that reads this agent's argv can still recover it
                # (true isolation needs per-agent UIDs -- L0.2 follow-up).
                token = secrets.token_urlsafe(32)
                shim = self._shim_factory(self._mailbox_client, tid, token=token)
                mcp_url = await shim.start()
                server: dict[str, object] = {"type": "http", "url": mcp_url}
                server["headers"] = {"Authorization": f"Bearer {token}"}
                mcp_servers["mailbox"] = server
                allowed_tools += MAILBOX_TOOLS

            if mcp_servers:
                agent_kwargs["mcp_servers"] = mcp_servers
            agent_kwargs["allowed_tools"] = allowed_tools

            agent = self._agent_factory(**agent_kwargs)
            await agent.start()
            self._agents[tid] = agent
            if shim is not None:
                self._shims[tid] = shim
            self._reg.update(tid, status="running", pid=agent.pid)

            # Everything below must succeed for run_events' finally to become
            # the sole owner of agent/shim teardown. Any failure up to and
            # including create_task() falls through to the except below,
            # which must undo the dict commits above and terminate whatever
            # was already started -- otherwise the shim's live server/socket
            # and the agent subprocess leak forever (see Task 10 review).
            await self._sink.task_started(tid, repo, task)
            t = asyncio.create_task(self.run_events(tid, agent))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        except Exception as exc:
            self._agents.pop(tid, None)
            self._shims.pop(tid, None)
            self._reg.update(tid, status="failed")
            self._reg.log_audit(tid, "error", f"spawn failed: {exc}")
            if agent is not None:
                try:
                    await agent.kill()
                except Exception as kill_exc:
                    self._reg.log_audit(
                        tid, "error", f"agent kill failed on spawn error: {kill_exc}"
                    )
            if shim is not None:
                try:
                    await shim.stop()
                except Exception as stop_exc:
                    self._reg.log_audit(
                        tid,
                        "error",
                        f"mailbox shim stop failed on spawn error: {stop_exc}",
                    )
            raise

        return tid

    async def run_events(self, task_id: int, agent: AgentProcess) -> None:
        activity_started = False
        terminal = "done"
        saw_result = False
        summary = ""
        try:
            async for ev in agent.events():
                if ev.kind == "system" and ev.session_id:
                    self._reg.update(task_id, resume_token=ev.session_id)
                if ev.kind == "result":
                    saw_result = True
                    summary = ev.text
                    self._reg.update(
                        task_id,
                        resume_token=ev.session_id or self._task(task_id).resume_token,
                    )
                    terminal = "failed" if ev.is_error else "done"

                line = activity_line(ev)
                if line is not None:
                    await self._sink.activity(task_id, line)
                    activity_started = True

                milestone = milestone_message(ev)
                if milestone is not None:
                    await self._sink.milestone(task_id, milestone)

            if not saw_result and self._task(task_id).status != "killed":
                terminal = "failed"
                summary = f"agent exited without result (rc={agent.returncode})"
                self._reg.log_audit(
                    task_id,
                    "error",
                    f"{summary}: {agent.stderr_text[-500:]}",
                )
        except Exception as exc:
            terminal = "failed"
            summary = f"run_events crashed: {exc}"
            self._reg.log_audit(task_id, "error", summary)
        finally:
            if self._task(task_id).status == "killed":
                terminal = "killed"
            self._reg.update(task_id, status=terminal)
            self._agents.pop(task_id, None)
            shim = self._shims.pop(task_id, None)
            if shim is not None:
                try:
                    await shim.stop()
                except Exception as exc:
                    self._reg.log_audit(
                        task_id, "error", f"mailbox shim stop failed: {exc}"
                    )
            _ = activity_started  # activity presence is tracked by the queen
            await self._sink.done(task_id, terminal, summary)

    async def kill(self, task_id: int) -> bool:
        agent = self._agents.get(task_id)
        if agent is None:
            return False
        self._reg.update(task_id, status="killed")
        self._reg.log_audit(task_id, "kill", "manual kill")
        await agent.kill()
        return True

    async def panic(self) -> int:
        ids = list(self._agents.keys())
        for tid in ids:
            await self.kill(tid)
        self._reg.log_audit(None, "panic", f"killed {len(ids)} agents")
        return len(ids)
