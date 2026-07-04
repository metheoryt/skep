import subprocess
import sys
from pathlib import Path

import pytest

FAKE_CLAUDE = Path(__file__).parent / "fixtures" / "fake_claude.py"


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
