from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from fleetd.config import Config
from fleetd.db import Registry
from fleetd.stream import Event
from fleetd.supervisor import Supervisor


def _cfg(tmp_path):
    return Config("tok", 42, -1001, tmp_path / "repos", tmp_path / "wt")


class FakeAgent:
    def __init__(self, events):
        self._events = events
        self.pid = 123
        self.killed = False
        self.started = False

    async def start(self):
        self.started = True

    async def events(self):
        for ev in self._events:
            yield ev

    async def kill(self):
        self.killed = True


def _gateway():
    gw = MagicMock()
    gw.create_topic = AsyncMock(return_value=555)
    gw.post = AsyncMock(return_value=9)
    gw.edit = AsyncMock()
    gw.delete_topic = AsyncMock()
    return gw


async def test_spawn_creates_worktree_topic_and_task(tmp_path):
    cfg = _cfg(tmp_path)
    (cfg.repos_root / "nix").mkdir(parents=True)
    reg = Registry.open(":memory:")
    gw = _gateway()
    created = {}

    def wt_factory(repo_path, worktree_path, branch):
        created["repo_path"] = repo_path
        created["branch"] = branch

    agent = FakeAgent([Event(kind="system", session_id="s9")])
    sup = Supervisor(cfg, reg, gw,
                     agent_factory=lambda **k: agent,
                     worktree_factory=wt_factory)
    tid = await sup.spawn("nix", "clean nvidia")

    task = reg.get_task(tid)
    assert task.repo == "nix"
    assert task.topic_id == 555
    assert created["repo_path"] == cfg.repos_root / "nix"
    gw.create_topic.assert_awaited_once()


async def test_run_events_edits_activity_and_marks_done(tmp_path):
    cfg = _cfg(tmp_path)
    reg = Registry.open(":memory:")
    gw = _gateway()
    events = [
        Event(kind="system", session_id="s9"),
        Event(kind="assistant_text", text="hi"),
        Event(kind="tool_use", tool_name="edit_file"),
        Event(kind="result", text="finished", is_error=False),
    ]
    agent = FakeAgent(events)
    sup = Supervisor(cfg, reg, gw,
                     agent_factory=lambda **k: agent,
                     worktree_factory=lambda *a, **k: None)
    tid = reg.add_task("nix", "t", str(tmp_path / "wt"))
    reg.update(tid, topic_id=555)

    await sup.run_events(tid, agent)

    task = reg.get_task(tid)
    assert task.status == "done"
    assert task.session_id == "s9"
    assert gw.edit.await_count >= 1          # activity updated
    assert gw.post.await_count >= 1          # milestone posted


async def test_kill_unknown_returns_false(tmp_path):
    sup = Supervisor(_cfg(tmp_path), Registry.open(":memory:"), _gateway())
    assert await sup.kill(999) is False


async def test_panic_kills_all_active(tmp_path):
    cfg = _cfg(tmp_path)
    reg = Registry.open(":memory:")
    sup = Supervisor(cfg, reg, _gateway())
    a1, a2 = FakeAgent([]), FakeAgent([])
    t1 = reg.add_task("r", "a", "/wt/a"); reg.update(t1, status="running")
    t2 = reg.add_task("r", "b", "/wt/b"); reg.update(t2, status="running")
    sup._agents = {t1: a1, t2: a2}          # inject live agents
    n = await sup.panic()
    assert n == 2
    assert a1.killed and a2.killed
