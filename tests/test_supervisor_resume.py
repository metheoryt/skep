import pytest
from conftest import FakeAgent

from skep.db import Registry
from skep.supervisor import Supervisor


@pytest.mark.asyncio
async def test_resume_starts_new_invocation_same_worktree(
    worker_config_no_memory, fake_sink
):
    created = {}

    def fake_agent(**kwargs):
        created.update(kwargs)
        return FakeAgent()

    reg = Registry.open(":memory:")
    first = reg.add_task("nix", "t", "/wt/nix-1")
    reg.update(first, session_local_id=first, resume_token="tok-1", status="done")

    sup = Supervisor(
        worker_config_no_memory, reg, fake_sink,
        agent_factory=fake_agent, worktree_factory=lambda *a: None,
    )
    second = await sup.resume(first, model="claude-opus-4-8")

    task = reg.get_task(second)
    assert second != first
    assert task.session_local_id == first          # same session
    assert task.worktree_path == "/wt/nix-1"        # same worktree, no new one
    assert created["resume_token"] == "tok-1"
    assert created["model"] == "claude-opus-4-8"
    assert created["cwd"].as_posix().endswith("/wt/nix-1")


@pytest.mark.asyncio
async def test_resume_unknown_session_raises(worker_config_no_memory, fake_sink):
    reg = Registry.open(":memory:")
    sup = Supervisor(
        worker_config_no_memory, reg, fake_sink, worktree_factory=lambda *a: None
    )
    with pytest.raises(ValueError):
        await sup.resume(12345)
