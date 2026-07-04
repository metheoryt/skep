from __future__ import annotations

import asyncio
import re

from fleetd.agent import AgentProcess, create_worktree
from fleetd.config import Config
from fleetd.db import Registry, Task
from fleetd.formatting import activity_line, milestone_message
from fleetd.telegram_gw import Gateway


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:24] or "task"


class Supervisor:
    def __init__(self, config: Config, registry: Registry, gateway: Gateway,
                 agent_factory=AgentProcess, worktree_factory=create_worktree):
        self._cfg = config
        self._reg = registry
        self._gw = gateway
        self._agent_factory = agent_factory
        self._worktree_factory = worktree_factory
        self._agents: dict[int, AgentProcess] = {}

    def list_active(self) -> list[Task]:
        return self._reg.list_active()

    async def spawn(self, repo: str, task_text: str) -> int:
        repo_path = self._cfg.repos_root / repo
        # reserve an id so the worktree/branch names are unique
        tid = self._reg.add_task(repo, task_text, "", mode="native")
        branch = f"fleetd/{_slug(task_text)}-{tid}"
        worktree_path = self._cfg.worktrees_root / f"{repo}-{tid}"
        self._reg.update(tid, worktree_path=str(worktree_path))

        self._worktree_factory(repo_path, worktree_path, branch)
        topic_id = await self._gw.create_topic(f"{repo} · {_slug(task_text)}")
        self._reg.update(tid, topic_id=topic_id)
        self._reg.log_audit(tid, "spawn", f"{repo}: {task_text}")

        agent = self._agent_factory(
            task_text=task_text, cwd=worktree_path, claude_bin=self._cfg.claude_bin
        )
        await agent.start()
        self._agents[tid] = agent
        self._reg.update(tid, status="running", pid=agent.pid)

        asyncio.create_task(self.run_events(tid, agent))
        return tid

    async def run_events(self, task_id: int, agent: AgentProcess) -> None:
        topic_id = self._reg.get_task(task_id).topic_id
        activity_msg_id: int | None = None
        terminal = "done"
        try:
            async for ev in agent.events():
                if ev.kind == "system" and ev.session_id:
                    self._reg.update(task_id, session_id=ev.session_id)
                if ev.kind == "result":
                    self._reg.update(task_id, session_id=ev.session_id or
                                     self._reg.get_task(task_id).session_id)
                    terminal = "failed" if ev.is_error else "done"

                line = activity_line(ev)
                if line is not None:
                    if activity_msg_id is None:
                        activity_msg_id = await self._gw.post(topic_id, line)
                    else:
                        await self._gw.edit(topic_id, activity_msg_id, line)

                milestone = milestone_message(ev)
                if milestone is not None:
                    await self._gw.post(topic_id, milestone)
        finally:
            if self._reg.get_task(task_id).status == "killed":
                terminal = "killed"
            self._reg.update(task_id, status=terminal)
            self._agents.pop(task_id, None)

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
