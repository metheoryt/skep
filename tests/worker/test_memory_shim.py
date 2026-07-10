import sys
from pathlib import Path

import pytest

from skep.memory import parse_fact
from skep.worker.memory_shim import MEMORY_TOOLS, build_remember, memory_shim_server


def test_server_entry_is_stdio_with_repo_path_as_argv(tmp_path):
    entry = memory_shim_server(tmp_path)
    assert entry["type"] == "stdio"
    assert entry["command"] == sys.executable
    # The parent repo path reaches the tool as a command-line argument, so the
    # agent cannot influence which repo it writes to (spec §5.2).
    assert entry["args"] == ["-m", "skep.worker.memory_shim", str(tmp_path)]


def test_no_port_and_no_token_in_entry(tmp_path):
    entry = memory_shim_server(tmp_path)
    assert "url" not in entry and "headers" not in entry


def test_grant_names_are_exact_not_wildcards():
    # spec §8.1 carry-forward 2: enumerate, never glob.
    assert MEMORY_TOOLS == ("mcp__memory__remember",)
    assert not any("*" in t for t in MEMORY_TOOLS)


def test_remember_writes_a_file_and_returns_its_path(tmp_path):
    remember = build_remember(tmp_path)
    out = remember(title="Stack takes 90s", body="poll /healthz")
    p = Path(out)
    assert p.is_file()
    fact = parse_fact(p.stem, p.read_text())
    assert fact is not None and fact.title == "Stack takes 90s"


def test_remember_closes_over_repo_path_not_cwd(tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    remember = build_remember(tmp_path)
    p = Path(remember(title="T", body="b"))
    assert p.parent == tmp_path / ".agent-memory"


def test_remember_surfaces_rejection_as_an_error(tmp_path):
    # A failed write must reach the agent as a tool error, never a silent no-op
    # it believes succeeded. `supersedes` naming no existing memory is a genuine
    # rejection (unlike a traversal title, which slugify neutralizes into a safe
    # contained slug and writes successfully).
    remember = build_remember(tmp_path)
    with pytest.raises(ValueError):
        remember(title="Real title", body="b", supersedes="never-written")
    assert not (tmp_path / ".agent-memory").exists()  # rejected write leaves nothing


def test_remember_supersedes_is_wired_through(tmp_path):
    remember = build_remember(tmp_path)
    remember(title="Old fact", body="stale")
    remember(title="New fact", body="fresh", supersedes="old-fact")
    old = tmp_path / ".agent-memory" / "old-fact.md"
    assert parse_fact("old-fact", old.read_text()).superseded_by == "new-fact"
