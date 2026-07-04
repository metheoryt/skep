from pathlib import Path

from skep.config import WorkerConfig
from skep.worker.app import build_worker
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
