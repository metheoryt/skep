import pytest

from fleetd.agent import AgentProcess, create_worktree
from fleetd.stream import Event


def test_create_worktree_makes_a_new_branch(git_repo, tmp_path):
    wt = tmp_path / "wt"
    create_worktree(git_repo, wt, "fleetd/task-1")
    assert (wt / "README.md").exists()
    assert (wt / ".git").exists()


async def test_agent_streams_events_until_exit(tmp_path, fake_claude_cmd):
    agent = AgentProcess(task_text="clean nvidia", cwd=tmp_path,
                         claude_bin=fake_claude_cmd)
    await agent.start()
    kinds = [ev.kind async for ev in agent.events()]
    assert kinds == ["system", "assistant_text", "tool_use", "result"]
    assert agent.pid is not None


async def test_agent_kill_stops_process(tmp_path, fake_claude_cmd):
    agent = AgentProcess(task_text="t", cwd=tmp_path, claude_bin=fake_claude_cmd)
    await agent.start()
    await agent.kill()
    # draining events after kill must not hang
    _ = [ev async for ev in agent.events()]


def test_argv_omits_input_format_for_phase1(tmp_path):
    agent = AgentProcess(task_text="t", cwd=tmp_path, claude_bin="claude")
    argv = agent._argv()
    assert "--input-format" not in argv
    assert "--output-format" in argv
    assert "stream-json" in argv
