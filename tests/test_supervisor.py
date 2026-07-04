from pathlib import Path

import pytest

from skep.config import WorkerConfig
from skep.db import Registry
from skep.stream import Event
from skep.supervisor import CapacityError, Supervisor


def _cfg(tmp_path, max_concurrent=8):
    return WorkerConfig(
        host="g16", profile="work", claude_config_dir="/cfg",
        repos_root=tmp_path / "repos", worktrees_root=tmp_path / "wt",
        db_path=":memory:", max_concurrent=max_concurrent, claude_bin="claude",
    )


class FakeAgent:
    def __init__(self, events, config_dir=None):
        self._events = events
        self.pid = 123
        self.killed = False
        self.started = False
        self.config_dir = config_dir

    async def start(self):
        self.started = True

    async def events(self):
        for ev in self._events:
            yield ev

    async def kill(self):
        self.killed = True

    @property
    def returncode(self):
        return 0

    @property
    def stderr_text(self):
        return ""


class RecordingSink:
    def __init__(self):
        self.events = []

    async def task_started(self, local_id, repo, title):
        self.events.append(("started", local_id, repo, title))

    async def activity(self, local_id, line):
        self.events.append(("activity", local_id, line))

    async def milestone(self, local_id, text):
        self.events.append(("milestone", local_id, text))

    async def done(self, local_id, status, summary):
        self.events.append(("done", local_id, status, summary))


async def test_spawn_records_task_and_emits_task_started(tmp_path):
    cfg = _cfg(tmp_path)
    (cfg.repos_root / "nix").mkdir(parents=True)
    reg = Registry.open(":memory:")
    sink = RecordingSink()
    captured = {}

    def agent_factory(task_text, cwd, claude_bin, config_dir=None):
        captured["config_dir"] = config_dir
        captured["cwd"] = cwd
        return FakeAgent([Event(kind="system", session_id="s9")])

    sup = Supervisor(cfg, reg, sink, agent_factory=agent_factory,
                     worktree_factory=lambda *a, **k: None)
    tid = await sup.spawn("nix", "clean nvidia")

    task = reg.get_task(tid)
    assert task.repo == "nix"
    assert captured["config_dir"] == "/cfg"        # profile isolation wired through
    assert ("started", tid, "nix", "clean nvidia") in sink.events


async def test_run_events_emits_activity_milestone_done(tmp_path):
    cfg = _cfg(tmp_path)
    reg = Registry.open(":memory:")
    sink = RecordingSink()
    events = [
        Event(kind="system", session_id="s9"),
        Event(kind="assistant_text", text="hi"),
        Event(kind="tool_use", tool_name="edit_file"),
        Event(kind="result", text="finished", is_error=False),
    ]
    agent = FakeAgent(events)
    sup = Supervisor(cfg, reg, sink, agent_factory=lambda **k: agent,
                     worktree_factory=lambda *a, **k: None)
    tid = reg.add_task("nix", "t", str(tmp_path / "wt"))

    await sup.run_events(tid, agent)

    task = reg.get_task(tid)
    assert task.status == "done"
    assert task.session_id == "s9"
    kinds = [e[0] for e in sink.events]
    assert "activity" in kinds and "milestone" in kinds and "done" in kinds
    assert ("done", tid, "done", "finished") in sink.events


async def test_spawn_rejects_over_capacity(tmp_path):
    cfg = _cfg(tmp_path, max_concurrent=1)
    (cfg.repos_root / "nix").mkdir(parents=True)
    reg = Registry.open(":memory:")
    sup = Supervisor(cfg, reg, RecordingSink(),
                     agent_factory=lambda **k: FakeAgent([]),
                     worktree_factory=lambda *a, **k: None)
    await sup.spawn("nix", "one")  # fills the single slot (agent never finishes here)
    with pytest.raises(CapacityError):
        await sup.spawn("nix", "two")


async def test_kill_unknown_returns_false(tmp_path):
    sup = Supervisor(_cfg(tmp_path), Registry.open(":memory:"), RecordingSink())
    assert await sup.kill(999) is False


async def test_panic_kills_all_active(tmp_path):
    cfg = _cfg(tmp_path)
    reg = Registry.open(":memory:")
    sup = Supervisor(cfg, reg, RecordingSink())
    a1, a2 = FakeAgent([]), FakeAgent([])
    t1 = reg.add_task("r", "a", "/wt/a"); reg.update(t1, status="running")
    t2 = reg.add_task("r", "b", "/wt/b"); reg.update(t2, status="running")
    sup._agents = {t1: a1, t2: a2}
    n = await sup.panic()
    assert n == 2
    assert a1.killed and a2.killed
