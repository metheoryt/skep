import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from skep import wire
from skep.config import WorkerConfig
from skep.memory import MemoryStore
from skep.worker import app as worker_app
from skep.worker.app import build_worker, serve
from skep.transport import SwitchableEventSink, SwitchableMailboxClient
from skep.supervisor import Supervisor
from skep.ws_transport import WorkerWsClient


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
    mailbox shim and writes a mailbox entry into the agent's --mcp-config
    file on spawn -- it did NOT before this fix, since Supervisor's
    mailbox_client was never supplied.
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

    writes = []
    sup._mcp_config_writer = lambda wt, servers: (  # type: ignore[attr-defined]
        writes.append((wt, servers)) or wt / ".skep" / "mcp.json")

    tid = await sup.spawn("nix", "clean nvidia")

    assert len(shims) == 1
    assert writes[0][1]["mailbox"]["url"] == f"http://127.0.0.1:9/mcp?tid={tid}"
    assert captured["mcp_config_path"].endswith(".skep/mcp.json")
    assert "mcp_servers" not in captured

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


def _client(sup):
    switch = SwitchableEventSink()
    return WorkerWsClient(_wcfg(), sup, switch, secret="s")


async def test_on_command_forwards_roots_to_the_supervisor():
    sup = AsyncMock()
    client = _client(sup)
    ws = AsyncMock()
    roots = [{"name": "nix", "mode": "new", "access": "rw"}]

    await client._on_command(ws, wire.spawn_msg("nix", "t", roots))

    sup.spawn.assert_awaited_once_with("nix", "t", roots=roots)


async def test_a_refused_root_is_reported_as_a_spawn_rejection():
    from skep.worker.roots import RootError

    sup = AsyncMock()
    sup.spawn.side_effect = RootError("attach roots are not supported yet")
    client = _client(sup)
    ws = AsyncMock()

    await client._on_command(ws, wire.spawn_msg("nix", "t", [{"name": "nix"}]))

    sent = wire.decode(ws.send_str.await_args[0][0])
    assert sent["t"] == wire.SPAWN_REJECTED
    assert "attach" in sent["reason"]
