from pathlib import Path

import pytest

from skep.config import WorkerConfig
from skep.worker import app as worker_app
from skep.worker.app import build_worker, serve
from skep.transport import SwitchableEventSink
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
