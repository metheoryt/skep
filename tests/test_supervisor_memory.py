from skep.config import WorkerConfig
from skep.db import Registry
from skep.supervisor import Supervisor


def _cfg(tmp_path, memory_enabled=True):
    return WorkerConfig(
        host="g16", profile="work", claude_config_dir="/cfg",
        repos_root=tmp_path / "repos", worktrees_root=tmp_path / "wt",
        db_path=":memory:", max_concurrent=8, claude_bin="claude",
        memory_enabled=memory_enabled,
    )


class FakeAgent:
    pid = 123

    async def start(self):
        pass

    async def events(self):
        return
        yield  # pragma: no cover -- makes this an async generator

    async def kill(self):
        pass

    @property
    def returncode(self):
        return 0

    @property
    def stderr_text(self):
        return ""


class RecordingSink:
    """The four methods spawn/run_events actually call on an EventSink."""

    async def task_started(self, *a, **k):
        pass

    async def activity(self, *a, **k):
        pass

    async def milestone(self, *a, **k):
        pass

    async def done(self, *a, **k):
        pass


class StubMemory:
    """MemoryProbe stub: hands back a fixed addendum, records the repo path."""

    def __init__(self, addendum="## Memory\n"):
        self.addendum = addendum
        self.seen = []

    async def addendum_for(self, repo_path):
        self.seen.append(repo_path)
        return self.addendum


class RaisingMemory:
    async def addendum_for(self, repo_path):
        raise RuntimeError("probe exploded")


def _sup(tmp_path, memory, captured, memory_enabled=True):
    def agent_factory(**kwargs):
        captured.update(kwargs)
        return FakeAgent()

    return Supervisor(
        _cfg(tmp_path, memory_enabled=memory_enabled),
        Registry.open(":memory:"),
        RecordingSink(),
        agent_factory=agent_factory,
        worktree_factory=lambda *a, **k: None,
        memory=memory,
    )


async def test_spawn_passes_addendum_for_the_parent_repo(tmp_path):
    captured = {}
    mem = StubMemory("## Memory\nrecall\n")
    await _sup(tmp_path, mem, captured).spawn("skep", "do it")

    assert captured["append_system_prompt"] == "## Memory\nrecall\n"
    # The parent repo, NOT the worktree: gortex tracks the parent only.
    assert mem.seen == [tmp_path / "repos" / "skep"]
    assert captured["cwd"] != tmp_path / "repos" / "skep"


async def test_spawn_omits_addendum_when_memory_unavailable(tmp_path):
    captured = {}
    await _sup(tmp_path, StubMemory(addendum=None), captured).spawn("skep", "do it")
    assert "append_system_prompt" not in captured


async def test_spawn_omits_addendum_when_disabled_even_if_available(tmp_path):
    captured = {}
    mem = StubMemory("## Memory\n")
    sup = _sup(tmp_path, mem, captured, memory_enabled=False)
    await sup.spawn("skep", "do it")

    assert "append_system_prompt" not in captured
    assert mem.seen == []  # disabled means we don't even probe


async def test_spawn_omits_addendum_when_no_memory_probe_configured(tmp_path):
    captured = {}
    await _sup(tmp_path, None, captured).spawn("skep", "do it")
    assert "append_system_prompt" not in captured


async def test_spawn_succeeds_when_the_probe_raises(tmp_path):
    # The memory dependency is soft: nothing it does may fail a spawn.
    captured = {}
    tid = await _sup(tmp_path, RaisingMemory(), captured).spawn("skep", "do it")
    assert tid > 0
    assert "append_system_prompt" not in captured
