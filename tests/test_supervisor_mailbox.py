"""Tests for Supervisor wiring of the per-agent mailbox MCP shim (Task 10)."""

import asyncio

from skep.config import WorkerConfig
from skep.db import Registry
from skep.supervisor import Supervisor


def _fake_writer_factory(recorder):
    def _writer(worktree, mcp_servers):
        recorder.append((worktree, mcp_servers))
        return worktree / ".skep" / "mcp.json"
    return _writer


def _cfg(tmp_path, max_concurrent=8):
    return WorkerConfig(
        host="g16", profile="work", claude_config_dir="/cfg",
        repos_root=tmp_path / "repos", worktrees_root=tmp_path / "wt",
        db_path=":memory:", max_concurrent=max_concurrent, claude_bin="claude",
    )


class FakeAgent:
    def __init__(self, task_text, cwd, claude_bin, config_dir=None,
                 mcp_config_path=None, allowed_tools=None,
                 add_dirs=None, model=None, env_passthrough=()):
        self.task_text = task_text
        self.cwd = cwd
        self.claude_bin = claude_bin
        self.config_dir = config_dir
        self.mcp_config_path = mcp_config_path
        self.allowed_tools = allowed_tools
        self.add_dirs = add_dirs
        self.model = model
        self.env_passthrough = env_passthrough
        self.pid = 123
        self.killed = False
        self.started = False

    async def start(self):
        self.started = True

    async def events(self):
        if False:
            yield  # pragma: no cover - empty async generator

    async def kill(self):
        self.killed = True

    @property
    def returncode(self):
        return 0

    @property
    def stderr_text(self):
        return ""


class BlockingAgent(FakeAgent):
    """An agent whose events() only ends once kill() has been called."""

    async def events(self):
        while not self.killed:
            await asyncio.sleep(0.01)
        if False:
            yield  # pragma: no cover - keeps this an async generator


class RecordingSink:
    def __init__(self):
        self.events = []

    async def task_started(self, local_id, repo, title, session_local_id=None):
        self.events.append(("started", local_id, repo, title))

    async def activity(self, local_id, line):
        self.events.append(("activity", local_id, line))

    async def milestone(self, local_id, text):
        self.events.append(("milestone", local_id, text))

    async def done(self, local_id, status, summary, reset_at=None):
        self.events.append(("done", local_id, status, summary, reset_at))


class FakeShim:
    """Stands in for MailboxShim; records start/stop and the tid it was built for."""

    def __init__(self, client, tid, token=None):
        self.client = client
        self.tid = tid
        self.token = token
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True
        return f"http://127.0.0.1:9/mcp?tid={self.tid}"

    async def stop(self):
        self.stopped = True


class FakeMailboxClient:
    async def send(self, tid, to, subject, body, in_reply_to):
        raise NotImplementedError

    async def read(self, tid):
        return []


async def test_spawn_starts_shim_and_passes_mcp_url(tmp_path):
    cfg = _cfg(tmp_path)
    reg = Registry.open(":memory:")
    sink = RecordingSink()
    captured = {}
    shims = []
    writes = []

    def agent_factory(**kwargs):
        captured.update(kwargs)
        return FakeAgent(**kwargs)

    def shim_factory(client, tid, token=None):
        s = FakeShim(client, tid, token=token)
        shims.append(s)
        return s

    sup = Supervisor(
        cfg, reg, sink, agent_factory=agent_factory,
        worktree_factory=lambda *a, **k: None,
        mailbox_client=FakeMailboxClient(), shim_factory=shim_factory,
        mcp_config_writer=_fake_writer_factory(writes),
    )
    tid = await sup.spawn("nix", "clean nvidia")

    assert len(shims) == 1
    assert shims[0].started
    assert shims[0].tid == tid
    assert len(writes) == 1
    written_worktree, written_servers = writes[0]
    server = written_servers["mailbox"]
    assert server["url"] == f"http://127.0.0.1:9/mcp?tid={tid}"
    token = server["headers"]["Authorization"].removeprefix("Bearer ")
    assert isinstance(token, str) and token
    assert token == shims[0].token  # shim enforces the same token
    assert captured["mcp_config_path"] == str(written_worktree / ".skep" / "mcp.json")
    assert "mcp_servers" not in captured  # map never reaches the agent kwargs
    assert sup._shims[tid] is shims[0]

    # let the background run_events task drain and stop the shim.
    pending = list(sup._tasks)
    if pending:
        await asyncio.gather(*pending)
    assert shims[0].stopped
    assert tid not in sup._shims


async def test_spawn_without_mailbox_client_passes_no_mcp_url(tmp_path):
    cfg = _cfg(tmp_path)
    reg = Registry.open(":memory:")
    sink = RecordingSink()
    captured = {}
    writes = []

    def agent_factory(**kwargs):
        captured.update(kwargs)
        return FakeAgent(**kwargs)

    sup = Supervisor(
        cfg, reg, sink, agent_factory=agent_factory,
        worktree_factory=lambda *a, **k: None,
        mcp_config_writer=_fake_writer_factory(writes),
    )
    await sup.spawn("nix", "clean nvidia")

    # no mailbox entry in the written map; memory-only map still written
    assert len(writes) == 1
    _, written_servers = writes[0]
    assert "mailbox" not in written_servers
    assert "mcp_servers" not in captured
    assert sup._shims == {}


async def test_existing_agent_factory_signature_unaffected_when_no_mailbox(tmp_path):
    """Regression guard: an agent_factory that predates the mailbox/memory
    kwargs still works when mailbox_client is None -- as long as it accepts
    (and may ignore) `mcp_servers`/`allowed_tools`/`add_dirs`/`model`. These
    are NOT forced conditionally: `allowed_tools` (the BASE_TOOLS grant) and
    `add_dirs`/`model` (workspace rendering, Task 7) are passed on every
    spawn regardless of mailbox/memory state (spec §2.1), so a factory that
    does not accept them at all is no longer a supported shape -- only the
    absence of a "mailbox" mcp_servers entry is guaranteed here."""
    cfg = _cfg(tmp_path)
    reg = Registry.open(":memory:")
    sink = RecordingSink()
    captured = {}

    def agent_factory(task_text, cwd, claude_bin, config_dir=None,
                       mcp_config_path=None, allowed_tools=None,
                       add_dirs=None, model=None, env_passthrough=()):
        captured["config_dir"] = config_dir
        return FakeAgent(task_text, cwd, claude_bin, config_dir)

    sup = Supervisor(
        cfg, reg, sink, agent_factory=agent_factory,
        worktree_factory=lambda *a, **k: None,
        mcp_config_writer=_fake_writer_factory([]),
    )
    tid = await sup.spawn("nix", "clean nvidia")
    assert captured["config_dir"] == "/cfg"
    assert tid is not None


async def test_run_events_completion_stops_shim(tmp_path):
    cfg = _cfg(tmp_path)
    reg = Registry.open(":memory:")
    sink = RecordingSink()
    agent = FakeAgent("t", tmp_path / "wt", "claude")
    sup = Supervisor(cfg, reg, sink, agent_factory=lambda **k: agent,
                     worktree_factory=lambda *a, **k: None)
    tid = reg.add_task("nix", "t", str(tmp_path / "wt"))
    shim = FakeShim(None, tid)
    sup._agents[tid] = agent
    sup._shims[tid] = shim

    await sup.run_events(tid, agent)

    assert shim.stopped
    assert tid not in sup._shims


async def test_kill_leads_to_shim_stop(tmp_path):
    cfg = _cfg(tmp_path)
    reg = Registry.open(":memory:")
    sink = RecordingSink()
    shims = []

    def shim_factory(client, tid, token=None):
        s = FakeShim(client, tid, token=token)
        shims.append(s)
        return s

    sup = Supervisor(
        cfg, reg, sink, agent_factory=lambda **k: BlockingAgent(**k),
        worktree_factory=lambda *a, **k: None,
        mailbox_client=FakeMailboxClient(), shim_factory=shim_factory,
        mcp_config_writer=_fake_writer_factory([]),
    )
    tid = await sup.spawn("nix", "clean nvidia")

    assert not shims[0].stopped
    assert await sup.kill(tid) is True

    pending = list(sup._tasks)
    if pending:
        await asyncio.gather(*pending)

    assert shims[0].stopped
    assert tid not in sup._shims


async def test_spawn_agent_start_failure_stops_shim(tmp_path):
    """If agent construction/start fails after the shim is up, the shim must
    be stopped and no tid entry must be left in _shims/_agents."""
    cfg = _cfg(tmp_path)
    reg = Registry.open(":memory:")
    sink = RecordingSink()
    shims = []

    def shim_factory(client, tid, token=None):
        s = FakeShim(client, tid, token=token)
        shims.append(s)
        return s

    def agent_factory(**kwargs):
        raise RuntimeError("boom: agent construction failed")

    sup = Supervisor(
        cfg, reg, sink, agent_factory=agent_factory,
        worktree_factory=lambda *a, **k: None,
        mailbox_client=FakeMailboxClient(), shim_factory=shim_factory,
        mcp_config_writer=_fake_writer_factory([]),
    )

    try:
        await sup.spawn("nix", "clean nvidia")
        assert False, "spawn should have raised"
    except RuntimeError:
        pass

    assert len(shims) == 1
    assert shims[0].started
    assert shims[0].stopped
    assert sup._shims == {}
    assert sup._agents == {}
    assert sup._tasks == set()


async def test_spawn_sink_failure_stops_shim_and_agent(tmp_path):
    """If task_started() (called after agent/shim are committed to the dicts)
    raises, spawn must terminate the agent, stop the shim, and leave no tid
    entry in _shims/_agents -- run_events never gets scheduled to do it."""
    cfg = _cfg(tmp_path)
    reg = Registry.open(":memory:")
    shims = []
    agents = []

    class FailingSink:
        async def task_started(self, local_id, repo, title, session_local_id=None):
            raise RuntimeError("boom: sink failed")

        async def activity(self, local_id, line):
            pass

        async def milestone(self, local_id, text):
            pass

        async def done(self, local_id, status, summary):
            pass

    def shim_factory(client, tid, token=None):
        s = FakeShim(client, tid, token=token)
        shims.append(s)
        return s

    def agent_factory(**kwargs):
        a = FakeAgent(**kwargs)
        agents.append(a)
        return a

    sup = Supervisor(
        cfg, reg, sink=FailingSink(), agent_factory=agent_factory,
        worktree_factory=lambda *a, **k: None,
        mailbox_client=FakeMailboxClient(), shim_factory=shim_factory,
        mcp_config_writer=_fake_writer_factory([]),
    )

    try:
        await sup.spawn("nix", "clean nvidia")
        assert False, "spawn should have raised"
    except RuntimeError:
        pass

    assert len(shims) == 1
    assert shims[0].stopped
    assert len(agents) == 1
    assert agents[0].killed
    assert sup._shims == {}
    assert sup._agents == {}
    assert sup._tasks == set()


async def test_shim_stop_failure_does_not_crash_run_events(tmp_path):
    """A stop() exception during teardown must be swallowed, not propagated."""
    cfg = _cfg(tmp_path)
    reg = Registry.open(":memory:")
    sink = RecordingSink()
    agent = FakeAgent("t", tmp_path / "wt", "claude")
    sup = Supervisor(cfg, reg, sink, agent_factory=lambda **k: agent,
                     worktree_factory=lambda *a, **k: None)
    tid = reg.add_task("nix", "t", str(tmp_path / "wt"))

    class ExplodingShim:
        async def stop(self):
            raise RuntimeError("boom")

    sup._agents[tid] = agent
    sup._shims[tid] = ExplodingShim()

    await sup.run_events(tid, agent)  # must not raise despite shim.stop() blowing up

    task = reg.get_task(tid)
    # this agent produced no "result" event, so run_events marks it failed on
    # its own merits -- the assertion here is that the ExplodingShim didn't
    # also propagate and crash the coroutine.
    assert task.status == "failed"
    assert tid not in sup._shims
