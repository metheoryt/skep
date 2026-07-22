import pytest
from conftest import FakeAgent, RecordingSink, _cfg

from skep.db import Registry
from skep.stream import Event
from skep.supervisor import CapacityError, Supervisor
from skep.worker.roots import RootError
from skep.workspace import ACCESS_RO, MODE_NEW, MODE_PRIMARY, Root, Workspace


async def test_spawn_records_task_and_emits_task_started(tmp_path):
    cfg = _cfg(tmp_path)
    (cfg.repos_root / "nix").mkdir(parents=True)
    reg = Registry.open(":memory:")
    sink = RecordingSink()
    captured = {}

    def agent_factory(task_text, cwd, claude_bin, config_dir=None,
                       mcp_config_path=None, allowed_tools=None,
                       add_dirs=None, model=None, env_passthrough=()):
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
    assert task.resume_token == "s9"
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


async def test_spawn_workspace_renders_multi_root_and_sets_session_local_id(
    worker_config_no_memory, fake_sink
):
    created = {}

    def fake_agent(**kwargs):
        created.update(kwargs)
        return FakeAgent([])

    reg = Registry.open(":memory:")
    sup = Supervisor(
        worker_config_no_memory, reg, fake_sink,
        agent_factory=fake_agent, worktree_factory=lambda *a: None,
    )
    ws = Workspace(
        roots=[
            Root("nix", worker_config_no_memory.repos_root / "nix", mode=MODE_NEW),
            Root(
                "main", worker_config_no_memory.repos_root / "main",
                mode=MODE_PRIMARY, access=ACCESS_RO,
            ),
        ]
    )
    tid = await sup.spawn_workspace(ws, "do the thing", model="claude-sonnet-5")

    task = reg.get_task(tid)
    assert task.session_local_id == tid          # first invocation keys to itself
    assert task.model == "claude-sonnet-5"
    assert created["add_dirs"] == [worker_config_no_memory.repos_root / "main"]
    assert created["model"] == "claude-sonnet-5"
    assert fake_sink.last_session_local_id == tid  # assert task_started emitted correct session_local_id


async def test_spawn_is_single_root_workspace(worker_config_no_memory, fake_sink):
    reg = Registry.open(":memory:")
    created = {}

    def fake_agent(**kwargs):
        created.update(kwargs)
        return FakeAgent([])

    sup = Supervisor(
        worker_config_no_memory, reg, fake_sink,
        agent_factory=fake_agent, worktree_factory=lambda *a: None,
    )
    tid = await sup.spawn("nix", "t")
    task = reg.get_task(tid)
    assert task.session_local_id == tid
    assert task.model is None
    assert created["add_dirs"] == []


async def test_spawn_with_roots_renders_add_dir(worker_config_no_memory, fake_sink):
    cfg = worker_config_no_memory
    (cfg.repos_root / "nix").mkdir(parents=True)
    reg = Registry.open(":memory:")
    created = {}

    def fake_agent(**kwargs):
        created.update(kwargs)
        return FakeAgent([])

    sup = Supervisor(
        cfg, reg, fake_sink,
        agent_factory=fake_agent, worktree_factory=lambda *a: None,
    )
    await sup.spawn(
        "nix",
        "clean up",
        roots=[
            {"name": "nix", "mode": "new", "access": "rw"},
            {"name": "nix", "mode": "primary", "access": "ro"},
        ],
    )
    assert created["add_dirs"] == [cfg.repos_root / "nix"]
    # cwd is still the fresh worktree, not the primary checkout
    assert created["cwd"].parent == cfg.worktrees_root


async def test_spawn_without_roots_is_unchanged(worker_config_no_memory, fake_sink):
    cfg = worker_config_no_memory
    (cfg.repos_root / "nix").mkdir(parents=True)
    reg = Registry.open(":memory:")
    created = {}

    def fake_agent(**kwargs):
        created.update(kwargs)
        return FakeAgent([])

    sup = Supervisor(
        cfg, reg, fake_sink,
        agent_factory=fake_agent, worktree_factory=lambda *a: None,
    )
    await sup.spawn("nix", "clean up")
    assert created["add_dirs"] == []
    assert created["cwd"].parent == cfg.worktrees_root


async def test_spawn_with_a_refused_root_raises_root_error(
    worker_config_no_memory, fake_sink
):
    cfg = worker_config_no_memory
    reg = Registry.open(":memory:")
    sup = Supervisor(
        cfg, reg, fake_sink,
        agent_factory=lambda **k: FakeAgent([]), worktree_factory=lambda *a: None,
    )
    with pytest.raises(RootError):
        await sup.spawn(
            "nix",
            "t",
            roots=[
                {"name": "nix", "mode": "new", "access": "rw"},
                {"name": "../etc", "mode": "primary", "access": "ro"},
            ],
        )


async def test_spawn_legacy_path_refuses_a_traversal_repo_name(
    worker_config_no_memory, fake_sink
):
    # The `roots is None` (legacy) branch of Supervisor.spawn took `repo`
    # verbatim from the wire and built a Workspace directly, skipping the
    # same name validation the `roots` branch enforces via resolve_roots. A
    # name like "../etc" must be refused here exactly as it already is on
    # the roots branch above -- one validator, both branches.
    cfg = worker_config_no_memory
    reg = Registry.open(":memory:")
    sup = Supervisor(
        cfg, reg, fake_sink,
        agent_factory=lambda **k: FakeAgent([]), worktree_factory=lambda *a: None,
    )
    with pytest.raises(RootError):
        await sup.spawn("../etc", "t")
