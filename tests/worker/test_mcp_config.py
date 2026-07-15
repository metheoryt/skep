import json
import os
import stat
import subprocess
from pathlib import Path

from skep.agent import create_worktree
from skep.worker.mcp_config import write_mcp_config

_SERVERS = {
    "mailbox": {"type": "http", "url": "http://127.0.0.1:5000/mcp",
                "headers": {"Authorization": "Bearer tok-123"}},
    "memory": {"type": "stdio", "command": "python",
               "args": ["-m", "skep.worker.memory_shim", "repo=/r"]},
}


def _make_worktree(git_repo, tmp_path):
    wt = tmp_path / "wt"
    create_worktree(git_repo, wt, "skep/task-1")
    return wt


def test_write_mcp_config_writes_whole_server_map(git_repo, tmp_path):
    wt = _make_worktree(git_repo, tmp_path)
    path = write_mcp_config(wt, _SERVERS)
    assert path == wt / ".skep" / "mcp.json"
    cfg = json.loads(path.read_text())
    assert set(cfg["mcpServers"]) == {"mailbox", "memory"}
    assert cfg["mcpServers"]["mailbox"]["headers"]["Authorization"] == "Bearer tok-123"
    assert cfg["mcpServers"]["memory"]["type"] == "stdio"


def test_write_mcp_config_file_is_0600(git_repo, tmp_path):
    wt = _make_worktree(git_repo, tmp_path)
    path = write_mcp_config(wt, _SERVERS)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(wt / ".skep").st_mode) == 0o700


def test_write_mcp_config_0600_even_when_file_pre_exists_loose(git_repo, tmp_path):
    # The persistent-repo (attach-root) case: a stale looser-perm mcp.json must
    # not leave the token readable. fchmod-before-write is the guarantee.
    wt = _make_worktree(git_repo, tmp_path)
    skep_dir = wt / ".skep"
    skep_dir.mkdir(parents=True, exist_ok=True)
    stale = skep_dir / "mcp.json"
    stale.write_text("{}")
    os.chmod(stale, 0o644)
    path = write_mcp_config(wt, _SERVERS)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_write_mcp_config_excludes_skep_dir_from_git(git_repo, tmp_path):
    wt = _make_worktree(git_repo, tmp_path)
    write_mcp_config(wt, _SERVERS)
    out = subprocess.run(
        ["git", "-C", str(wt), "check-ignore", ".skep/mcp.json"],
        capture_output=True, text=True,
    )
    assert out.returncode == 0


def test_write_mcp_config_exclude_is_idempotent(git_repo, tmp_path):
    wt = _make_worktree(git_repo, tmp_path)
    write_mcp_config(wt, _SERVERS)
    write_mcp_config(wt, _SERVERS)  # second call
    exclude_rel = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--git-path", "info/exclude"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    exclude_path = Path(exclude_rel) if Path(exclude_rel).is_absolute() else wt / exclude_rel
    assert exclude_path.read_text().count("/.skep/") == 1


def test_write_mcp_config_survives_non_git_worktree(tmp_path):
    wt = tmp_path / "plain"
    wt.mkdir()
    path = write_mcp_config(wt, _SERVERS)
    assert path.exists()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
