from __future__ import annotations

import asyncio
import re

from fleetd.agent import AgentProcess, create_worktree
from fleetd.config import WorkerConfig
from fleetd.db import Registry, Task
from fleetd.formatting import activity_line, milestone_message
from fleetd.transport import EventSink


class CapacityError(Exception):
    """Raised when a worker is at max_concurrent and cannot accept a new task."""


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:24] or "task"


class Supervisor:
    def __init__(self, config: WorkerConfig, registry: Registry, sink: EventSink,
                 agent_factory=AgentProcess, worktree_factory=create_worktree):
        self._cfg = config
        self._reg = registry
        self._sink = sink
        self._agent_factory = agent_factory
        self._worktree_factory = worktree_factory
        self._agents: dict[int, AgentProcess] = {}
        self._tasks: set[asyncio.Task] = set()

    def list_active(self) -> list[Task]:
        return self._reg.list_active()

    async def spawn(self, repo: str, task: str) -> int:
        if len(self._agents) >= self._cfg.max_concurrent:
            raise CapacityError(
                f"at capacity ({self._cfg.max_concurrent} running)"
            )
        repo_path = self._cfg.repos_root / repo
        tid = self._reg.add_task(repo, task, "", mode="native")
        branch = f"fleetd/{_slug(task)}-{tid}"
        worktree_path = self._cfg.worktrees_root / f"{repo}-{tid}"
        self._reg.update(tid, worktree_path=str(worktree_path))

        try:
            self._worktree_factory(repo_path, worktree_path, branch)
            self._reg.log_audit(tid, "spawn", f"{repo}: {task}")
            agent = self._agent_factory(
                task_text=task, cwd=worktree_path,
                claude_bin=self._cfg.claude_bin,
                config_dir=self._cfg.claude_config_dir,
            )
            await agent.start()
            self._agents[tid] = agent
            self._reg.update(tid, status="running", pid=agent.pid)
        except Exception as exc:
            self._reg.update(tid, status="failed")
            self._reg.log_audit(tid, "error", f"spawn failed: {exc}")
            raise

        await self._sink.task_started(tid, repo, task)
        t = asyncio.create_task(self.run_events(tid, agent))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return tid

    async def run_events(self, task_id: int, agent: AgentProcess) -> None:
        activity_started = False
        terminal = "done"
        saw_result = False
        summary = ""
        try:
            async for ev in agent.events():
                if ev.kind == "system" and ev.session_id:
                    self._reg.update(task_id, session_id=ev.session_id)
                if ev.kind == "result":
                    saw_result = True
                    summary = ev.text
                    self._reg.update(
                        task_id,
                        session_id=ev.session_id
                        or self._reg.get_task(task_id).session_id,
                    )
                    terminal = "failed" if ev.is_error else "done"

                line = activity_line(ev)
                if line is not None:
                    await self._sink.activity(task_id, line)
                    activity_started = True

                milestone = milestone_message(ev)
                if milestone is not None:
                    await self._sink.milestone(task_id, milestone)

            if not saw_result and self._reg.get_task(task_id).status != "killed":
                terminal = "failed"
                summary = f"agent exited without result (rc={agent.returncode})"
                self._reg.log_audit(
                    task_id, "error",
                    f"{summary}: {agent.stderr_text[-500:]}",
                )
        except Exception as exc:
            terminal = "failed"
            summary = f"run_events crashed: {exc}"
            self._reg.log_audit(task_id, "error", summary)
        finally:
            if self._reg.get_task(task_id).status == "killed":
                terminal = "killed"
            self._reg.update(task_id, status=terminal)
            self._agents.pop(task_id, None)
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
