import pytest
from pathlib import Path

from skep.agent import AgentProcess, _agent_env, create_worktree
from skep.stream import Event


def test_agent_env_injects_config_dir(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    env = _agent_env("/home/me/.claude-work")
    assert env["CLAUDE_CONFIG_DIR"] == "/home/me/.claude-work"
    assert env["PATH"] == "/usr/bin"  # base env preserved


def test_agent_env_none_leaves_config_dir_unset(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    env = _agent_env(None)
    assert "CLAUDE_CONFIG_DIR" not in env


def test_create_worktree_makes_a_new_branch(git_repo, tmp_path):
    wt = tmp_path / "wt"
    create_worktree(git_repo, wt, "skep/task-1")
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


def test_argv_renders_add_dir_model_resume(tmp_path):
    agent = AgentProcess(
        task_text="do it",
        cwd=tmp_path,
        claude_bin="claude",
        add_dirs=[Path("/repos/main"), Path("/repos/shared")],
        model="claude-sonnet-5",
        resume_token="sess-xyz",
    )
    argv = agent._argv()
    assert argv[:2] == ["claude", "-p"]
    assert argv.count("--add-dir") == 2
    assert "/repos/main" in argv and "/repos/shared" in argv
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    assert argv[argv.index("--resume") + 1] == "sess-xyz"


def test_argv_omits_new_flags_by_default(tmp_path):
    agent = AgentProcess(task_text="t", cwd=tmp_path, claude_bin="claude")
    argv = agent._argv()
    assert "--add-dir" not in argv
    assert "--model" not in argv
    assert "--resume" not in argv


def test_agent_env_drops_secrets_and_session_markers(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("SKEP_SHARED_SECRET", "hunter2")
    monkeypatch.setenv("SKEP_DB", "/var/skep.sqlite")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-nope")
    env = _agent_env("/home/me/.claude-work")
    assert "SKEP_SHARED_SECRET" not in env
    assert "SKEP_DB" not in env
    assert "CLAUDE_CODE_ENTRYPOINT" not in env
    assert "CLAUDECODE" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["CLAUDE_CONFIG_DIR"] == "/home/me/.claude-work"


def test_agent_env_keeps_lc_and_optional_when_present(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/cert.pem")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    env = _agent_env(None)
    assert env["LC_ALL"] == "en_US.UTF-8"
    assert env["SSL_CERT_FILE"] == "/etc/ssl/cert.pem"
    assert env["NO_PROXY"] == "127.0.0.1"


def test_agent_env_never_inherits_config_dir_when_none(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/worker/own/cfg")
    env = _agent_env(None)
    assert "CLAUDE_CONFIG_DIR" not in env  # worker identity must not leak


def test_agent_env_honors_passthrough(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("MY_HOST_VAR", "keepme")
    monkeypatch.setenv("SKEP_SHARED_SECRET", "hunter2")
    env = _agent_env(None, passthrough=("MY_HOST_VAR",))
    assert env["MY_HOST_VAR"] == "keepme"
    assert "SKEP_SHARED_SECRET" not in env  # passthrough doesn't reopen the namespace


def test_agent_env_passthrough_missing_key_is_noop(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    env = _agent_env(None, passthrough=("NOT_SET_ANYWHERE",))
    assert "NOT_SET_ANYWHERE" not in env
