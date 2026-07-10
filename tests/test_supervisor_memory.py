from skep.config import WorkerConfig
from skep.db import Registry
from skep.supervisor import BASE_TOOLS, MAILBOX_TOOLS, Supervisor


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
    # The parent repo, NOT the worktree: a fact must outlive a killed task.
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


def test_base_tools_grant_write_but_not_read():
    # spec §2.3: Bash,Edit,Write on argv; Read needs no grant.
    assert BASE_TOOLS == ("Bash", "Edit", "Write")
    assert "Read" not in BASE_TOOLS


def test_mailbox_tools_are_enumerated_not_globbed():
    # spec §8.1 carry-forward 2: the wildcard was never validated.
    assert MAILBOX_TOOLS == ("mcp__mailbox__send_message", "mcp__mailbox__read_inbox")
    assert not any("*" in t for t in MAILBOX_TOOLS)


class FakeMailboxClient:
    """Enough of MailboxClient for Supervisor to take the shim path."""


def _sup_with_mailbox(tmp_path, captured):
    shims = []

    class FakeShim:
        def __init__(self, client, tid, token):
            self.token = token
            shims.append(self)

        async def start(self):
            return "http://127.0.0.1:9/mcp"

        async def stop(self):
            pass

    def agent_factory(**kwargs):
        captured.update(kwargs)
        return FakeAgent()

    return Supervisor(
        _cfg(tmp_path),
        Registry.open(":memory:"),
        RecordingSink(),
        agent_factory=agent_factory,
        worktree_factory=lambda *a, **k: None,
        mailbox_client=FakeMailboxClient(),
        shim_factory=FakeShim,
        memory=StubMemory(),
    )


async def test_memory_server_present_when_mailbox_is_off(tmp_path):
    captured = {}
    await _sup(tmp_path, StubMemory(), captured).spawn("skep", "do it")
    assert set(captured["mcp_servers"]) == {"memory"}
    assert captured["mcp_servers"]["memory"]["type"] == "stdio"
    assert "mcp__memory__remember" in captured["allowed_tools"]


async def test_both_servers_when_mailbox_is_on(tmp_path):
    captured = {}
    await _sup_with_mailbox(tmp_path, captured).spawn("skep", "do it")
    assert set(captured["mcp_servers"]) == {"memory", "mailbox"}
    assert captured["mcp_servers"]["memory"]["type"] == "stdio"
    assert captured["mcp_servers"]["mailbox"]["type"] == "http"
    assert "mcp__memory__remember" in captured["allowed_tools"]
    assert "mcp__mailbox__send_message" in captured["allowed_tools"]


async def test_memory_disabled_omits_server_and_grant(tmp_path):
    captured = {}
    sup = _sup(tmp_path, StubMemory(), captured, memory_enabled=False)
    await sup.spawn("skep", "do it")
    assert "memory" not in (captured.get("mcp_servers") or {})
    assert "mcp__memory__remember" not in captured["allowed_tools"]
    assert "append_system_prompt" not in captured


async def test_baseline_grant_present_even_when_memory_disabled(tmp_path):
    # The coding baseline is not memory's to withhold: agents could not write
    # files at all before this plan (spec §2.1).
    captured = {}
    sup = _sup(tmp_path, StubMemory(), captured, memory_enabled=False)
    await sup.spawn("skep", "do it")
    assert captured["allowed_tools"] == list(BASE_TOOLS)


async def test_read_failure_still_yields_shim_and_grant(tmp_path):
    # spec §6: read failure does not disable writing. RaisingMemory already
    # exists in this file.
    captured = {}
    await _sup(tmp_path, RaisingMemory(), captured).spawn("skep", "do it")
    assert "append_system_prompt" not in captured
    assert "memory" in captured["mcp_servers"]
    assert "mcp__memory__remember" in captured["allowed_tools"]


async def test_shim_is_pointed_at_the_parent_repo_not_the_worktree(tmp_path):
    captured = {}
    await _sup(tmp_path, StubMemory(), captured).spawn("skep", "do it")
    args = captured["mcp_servers"]["memory"]["args"]
    # The repo path arrives as argv, so the agent cannot redirect the write.
    assert args[-1] == str(tmp_path / "repos" / "skep")
    assert args[-1] != str(captured["cwd"])
