from skep.config import WorkerConfig
from skep.db import Registry
from skep.memory import MemoryStore, write_memory
from skep.supervisor import BASE_TOOLS, MAILBOX_TOOLS, Supervisor
from skep.worker.memory_shim import MEMORY_TOOLS


def _fake_writer_factory(recorder):
    def _writer(worktree, mcp_servers):
        recorder.append((worktree, mcp_servers))
        return worktree / ".skep" / "mcp.json"
    return _writer


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
    """MemoryProbe stub: hands back a fixed addendum, records the root paths."""

    def __init__(self, addendum="## Memory\n"):
        self.addendum = addendum
        self.seen = []

    async def addendum_for(self, root_paths):
        self.seen.append(root_paths)
        return self.addendum


class RaisingMemory:
    async def addendum_for(self, root_paths):
        raise RuntimeError("probe exploded")


def _sup(tmp_path, memory, captured, memory_enabled=True, writes=None):
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
        mcp_config_writer=_fake_writer_factory(writes if writes is not None else []),
    )


async def test_spawn_passes_addendum_for_the_parent_repo(tmp_path):
    captured = {}
    mem = StubMemory("## Memory\nrecall\n")
    await _sup(tmp_path, mem, captured).spawn("skep", "do it")

    assert captured["append_system_prompt"] == "## Memory\nrecall\n"
    # The parent repo, NOT the worktree: a fact must outlive a killed task.
    assert mem.seen == [[tmp_path / "repos" / "skep"]]
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


def _sup_with_mailbox(tmp_path, captured, writes=None):
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
        mcp_config_writer=_fake_writer_factory(writes if writes is not None else []),
    )


async def test_memory_server_present_when_mailbox_is_off(tmp_path):
    captured = {}
    writes = []
    await _sup(tmp_path, StubMemory(), captured, writes=writes).spawn("skep", "do it")
    _, written_servers = writes[0]
    assert set(written_servers) == {"memory"}
    assert written_servers["memory"]["type"] == "stdio"
    assert "mcp__memory__remember" in captured["allowed_tools"]


async def test_both_servers_when_mailbox_is_on(tmp_path):
    captured = {}
    writes = []
    await _sup_with_mailbox(tmp_path, captured, writes=writes).spawn("skep", "do it")
    _, written_servers = writes[0]
    assert set(written_servers) == {"memory", "mailbox"}
    assert written_servers["memory"]["type"] == "stdio"
    assert written_servers["mailbox"]["type"] == "http"
    # Pin the COMPOSED grant exactly, in assembly order (BASE, MEMORY, MAILBOX).
    # A loose `in` check would pass a widened assembly -- a stray "Read", a
    # wildcard, a duplicate. The grant is the security boundary; assert it whole.
    assert captured["allowed_tools"] == [*BASE_TOOLS, *MEMORY_TOOLS, *MAILBOX_TOOLS]


async def test_memory_disabled_omits_server_and_grant(tmp_path):
    captured = {}
    writes = []
    sup = _sup(tmp_path, StubMemory(), captured, memory_enabled=False, writes=writes)
    await sup.spawn("skep", "do it")
    # memory is the only source with mailbox off; disabled => no map => no write.
    assert writes == []
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
    writes = []
    await _sup(tmp_path, RaisingMemory(), captured, writes=writes).spawn("skep", "do it")
    assert "append_system_prompt" not in captured
    _, written_servers = writes[0]
    assert "memory" in written_servers
    assert "mcp__memory__remember" in captured["allowed_tools"]


async def test_shim_is_pointed_at_the_parent_repo_not_the_worktree(tmp_path):
    captured = {}
    writes = []
    await _sup(tmp_path, StubMemory(), captured, writes=writes).spawn("skep", "do it")
    _, written_servers = writes[0]
    args = written_servers["memory"]["args"]
    # The repo path arrives as argv (embedded in a name=path pair), so the
    # agent cannot redirect the write.
    assert args[-1] == f"skep={tmp_path / 'repos' / 'skep'}"
    assert args[-1] != f"skep={captured['cwd']}"


_RO_ROOTS = [
    {"name": "nix", "mode": "new", "access": "rw"},
    {"name": "watched", "mode": "primary", "access": "ro"},
]
"""One rw root the agent owns (a fresh worktree of `nix`) plus one ro root
(the operator's live checkout of `watched`) -- distinct names so the two
paths never collide, which is what makes the rw/ro split observable below.
"""


async def test_memory_shim_never_receives_a_read_only_root(tmp_path):
    captured = {}
    writes = []
    await _sup(tmp_path, StubMemory(), captured, writes=writes).spawn(
        "nix", "t", roots=_RO_ROOTS
    )
    _, written_servers = writes[0]
    memory_args = " ".join(written_servers["memory"]["args"])
    assert f"nix={tmp_path / 'repos' / 'nix'}" in memory_args
    assert "watched=" not in memory_args


async def test_addendum_still_reads_the_read_only_root(tmp_path):
    captured = {}
    mem = StubMemory()
    await _sup(tmp_path, mem, captured).spawn("nix", "t", roots=_RO_ROOTS)
    # The addendum read must union every root, ro included -- reading the
    # watched checkout's memory is the entire point of watching it.
    assert mem.seen == [
        [tmp_path / "repos" / "nix", tmp_path / "repos" / "watched"]
    ]


async def test_prompt_carries_the_read_only_declaration(tmp_path):
    captured = {}
    await _sup(tmp_path, StubMemory(), captured).spawn("nix", "t", roots=_RO_ROOTS)
    assert "READ-ONLY" in captured["append_system_prompt"]


async def test_no_declaration_when_no_read_only_root(tmp_path):
    captured = {}
    await _sup(tmp_path, StubMemory(), captured).spawn("skep", "do it")
    assert "READ-ONLY" not in captured.get("append_system_prompt", "")


_SAME_NAME_WATCH_ROOTS = [
    {"name": "nix", "mode": "new", "access": "rw"},
    {"name": "nix", "mode": "primary", "access": "ro"},
]
"""The canonical `/spawn <host> <repo> --watch <task>` shape: same name for
both the agent's own (rw) root and the watched (ro) root. resolve_roots maps
a name to repos_root/<name>, so the two entries resolve to the IDENTICAL
path -- this is what the rw-path exclusion in readonly_declaration exists
for, and what makes the memory shim's name-keyed root map collapse below.
"""


async def test_memory_shim_root_map_collapses_for_same_name_watch(tmp_path):
    """Same-name --watch spawn: the rw and ro roots share both name and
    resolved path, so the memory shim's (name-keyed) root map ends up with a
    single `nix` entry pointing at the parent repo either way -- the
    rw-only write filter is inert here because the duplicate already
    collapsed before it could matter. This documents that collapse rather
    than asserting some stronger isolation that the code does not provide.
    """
    captured = {}
    writes = []
    await _sup(tmp_path, StubMemory(), captured, writes=writes).spawn(
        "nix", "t", roots=_SAME_NAME_WATCH_ROOTS
    )
    _, written_servers = writes[0]
    memory_args = written_servers["memory"]["args"]
    nix_entries = [a for a in memory_args if a.startswith("nix=")]
    assert nix_entries == [f"nix={tmp_path / 'repos' / 'nix'}"]


async def test_no_readonly_declaration_for_same_name_watch(tmp_path):
    """Fix under test: readonly_declaration must not tell the agent that
    repos_root/nix is READ-ONLY when skep's own rw memory shim is
    simultaneously configured to write .agent-memory/ files there. Since the
    rw and ro roots resolve to the same path, and that is the only ro root,
    no declaration is emitted at all.
    """
    captured = {}
    await _sup(tmp_path, StubMemory(), captured).spawn(
        "nix", "t", roots=_SAME_NAME_WATCH_ROOTS
    )
    assert "READ-ONLY" not in captured.get("append_system_prompt", "")


async def test_same_name_watch_renders_each_memory_fact_once(tmp_path):
    """Fix under test: the canonical same-name `--watch` shape must not
    duplicate every fact in the rendered addendum. The read list unions
    every root ON PURPOSE (test_addendum_still_reads_the_read_only_root
    above) -- but here both roots resolve to the IDENTICAL path, so a naive
    union reads (and renders) the same fact twice, silently halving the
    effective byte budget. Uses the real MemoryStore, not StubMemory: the
    dedupe lives in addendum_for, so a StubMemory.seen assertion would not
    exercise it.
    """
    repo_path = tmp_path / "repos" / "nix"
    (repo_path / ".agent-memory").mkdir(parents=True)
    write_memory({"nix": repo_path}, "nix", "Stack takes 90s", "body text", "gotcha")

    captured = {}
    await _sup(tmp_path, MemoryStore(), captured).spawn(
        "nix", "t", roots=_SAME_NAME_WATCH_ROOTS
    )
    prompt = captured["append_system_prompt"]
    assert prompt.count("Stack takes 90s") == 1
