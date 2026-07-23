import subprocess
import sys
from pathlib import Path

import pytest

from skep.config import WorkerConfig

FAKE_CLAUDE = Path(__file__).parent / "fixtures" / "fake_claude.py"


def _cfg(tmp_path, max_concurrent=8, memory_enabled=True):
    """Shared WorkerConfig builder for Supervisor tests.

    Moved from test_supervisor.py (Task 8 conftest refactor) so
    test_supervisor_resume.py can reuse it via `worker_config_no_memory`.
    """
    return WorkerConfig(
        host="g16", profile="work", claude_config_dir="/cfg",
        repos_root=tmp_path / "repos", worktrees_root=tmp_path / "wt",
        db_path=":memory:", max_concurrent=max_concurrent, claude_bin="claude",
        memory_enabled=memory_enabled,
    )


@pytest.fixture
def worker_config_no_memory(tmp_path):
    return _cfg(tmp_path, memory_enabled=False)


class FakeAgent:
    """Shared Supervisor test double: replays a fixed list of events.

    Moved from test_supervisor.py (Task 8 conftest refactor) so
    test_supervisor_resume.py can reuse it. `events` defaults to empty so
    `FakeAgent()` (no events, agent never produces a "result") works for
    tests -- like resume -- that don't care about the run_events replay.
    """

    def __init__(self, events=(), config_dir=None):
        self._events = events
        self.pid = 123
        self.killed = False
        self.started = False
        self.config_dir = config_dir

    async def start(self):
        self.started = True

    async def events(self):
        for ev in self._events:
            yield ev

    async def kill(self):
        self.killed = True

    @property
    def returncode(self):
        return 0

    @property
    def stderr_text(self):
        return ""


class RecordingSink:
    """Shared EventSink test double; records emitted events + last session_local_id.

    Moved from test_supervisor.py (Task 8 conftest refactor).
    """

    def __init__(self):
        self.events = []
        self.last_session_local_id = None

    async def task_started(self, local_id, repo, title, session_local_id=None):
        self.last_session_local_id = session_local_id
        self.events.append(("started", local_id, repo, title))

    async def activity(self, local_id, line):
        self.events.append(("activity", local_id, line))

    async def milestone(self, local_id, text):
        self.events.append(("milestone", local_id, text))

    async def done(self, local_id, status, summary, reset_at=None):
        self.events.append(("done", local_id, status, summary, reset_at))


@pytest.fixture
def fake_sink():
    return RecordingSink()


@pytest.fixture
def fake_claude_cmd():
    """A command string that behaves like `claude` for AgentProcess."""
    # AgentProcess runs [claude_bin, "-p", ...]. We point claude_bin at a
    # wrapper that ignores flags and runs the stub. Use a tiny shell shim.
    return f"{sys.executable} {FAKE_CLAUDE}"


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True,
                   env={**env, "PATH": __import__("os").environ["PATH"]})
    return repo
