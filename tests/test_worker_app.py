import asyncio
from pathlib import Path

import pytest

from skep.config import WorkerConfig
from skep.memory import MemoryStore
from skep.worker import app as worker_app
from skep.worker.app import build_worker, serve
from skep.transport import SwitchableEventSink, SwitchableMailboxClient
from skep.supervisor import Supervisor


def _wcfg(**kw):
    base = dict(
        host="g16", profile="work", claude_config_dir=None,
        repos_root=Path("/tmp"), worktrees_root=Path("/tmp"),
        db_path=":memory:", shared_secret="s",
    )
    base.update(kw)
    return WorkerConfig(**base)


def test_build_worker_wires_supervisor_to_switch():
    sup, switch, client = build_worker(_wcfg())
    assert isinstance(sup, Supervisor)
    assert isinstance(switch, SwitchableEventSink)
    # the supervisor's sink IS the switch, so reconnects can swap the target
    assert sup._sink is switch  # type: ignore[attr-defined]
    assert client is not None


def test_build_worker_activates_mailbox_end_to_end():
    """Merge-blocker regression: build_worker must wire ONE SwitchableMailboxClient
    into both the Supervisor (so spawn() starts a per-agent shim) and the
    WorkerWsClient (so run_once can set_target/clear it per WS connection).
    Before this fix, both constructors defaulted mailbox params to None and
    the mailbox feature was inert at runtime.
    """
    sup, _switch, client = build_worker(_wcfg())

    assert isinstance(sup._mailbox_client, SwitchableMailboxClient)  # type: ignore[attr-defined]
    assert sup._mailbox_client is client._mailbox_switch  # type: ignore[attr-defined]


async def test_build_worker_supervisor_starts_shim_on_spawn(tmp_path):
    """Behavioral proof: a REAL assembled worker (via build_worker) starts a
    mailbox shim and injects a mailbox mcp_servers entry into the agent on
    spawn -- it did NOT before this fix, since Supervisor's mailbox_client
    was never supplied.
    """
    sup, _switch, _client = build_worker(_wcfg(
        repos_root=tmp_path / "repos", worktrees_root=tmp_path / "wt"))
    sup._worktree_factory = lambda *a, **k: None  # type: ignore[attr-defined]

    captured: dict = {}
    shims = []

    class FakeShim:
        def __init__(self, client, tid):
            self.client = client
            self.tid = tid
            self.stopped = False

        async def start(self):
            return f"http://127.0.0.1:9/mcp?tid={self.tid}"

        async def stop(self):
            self.stopped = True

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def start(self):
            pass

        async def events(self):
            if False:
                yield  # pragma: no cover - empty async generator

        async def kill(self):
            pass

        @property
        def pid(self):
            return 1

        @property
        def returncode(self):
            return 0

        @property
        def stderr_text(self):
            return ""

    def shim_factory(client, tid, token=None):
        s = FakeShim(client, tid)
        shims.append(s)
        return s

    sup._agent_factory = lambda **kwargs: FakeAgent(**kwargs)  # type: ignore[attr-defined]
    sup._shim_factory = shim_factory  # type: ignore[attr-defined]

    tid = await sup.spawn("nix", "clean nvidia")

    assert len(shims) == 1
    assert captured["mcp_servers"]["mailbox"]["url"] == f"http://127.0.0.1:9/mcp?tid={tid}"

    pending = list(sup._tasks)  # type: ignore[attr-defined]
    if pending:
        await asyncio.gather(*pending)
    assert shims[0].stopped


async def test_serve_refuses_empty_shared_secret_before_any_io(monkeypatch):
    """Fail closed: an empty SKEP_SHARED_SECRET must abort serve() before
    resolving the queen URL or opening any connection."""
    wcfg = _wcfg(shared_secret="")

    async def _boom(*args, **kwargs):
        raise AssertionError("serve() attempted network I/O despite empty secret")

    monkeypatch.setattr(worker_app, "resolve_queen_url", _boom)
    monkeypatch.setattr(worker_app, "build_worker", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("serve() attempted to build_worker despite empty secret")))

    with pytest.raises(SystemExit, match="SKEP_SHARED_SECRET"):
        await serve(wcfg)


async def test_serve_refuses_whitespace_only_shared_secret(monkeypatch):
    wcfg = _wcfg(shared_secret="   ")

    async def _boom(*args, **kwargs):
        raise AssertionError("serve() attempted network I/O despite blank secret")

    monkeypatch.setattr(worker_app, "resolve_queen_url", _boom)

    with pytest.raises(SystemExit, match="SKEP_SHARED_SECRET"):
        await serve(wcfg)


def test_build_worker_gives_supervisor_a_real_memory_store():
    supervisor, _switch, _client = build_worker(_wcfg())
    assert isinstance(supervisor._memory, MemoryStore)  # type: ignore[attr-defined]
