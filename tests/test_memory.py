import logging
import shlex
import sys
from pathlib import Path

import pytest

from skep.memory import MemoryPreflight, memory_addendum, probe_memory, recall_command


def test_recall_command_indexes_the_parent_repo():
    assert recall_command(Path("/home/me/my/skep")) == [
        "gortex", "memory", "recall",
        "--index", "/home/me/my/skep",
        "--limit", "10",
    ]


def test_addendum_embeds_the_exact_probed_recall_command():
    # Spec test 5: the string the agent is told to run and the string the
    # preflight smoke-checks must never drift apart.
    repo = Path("/home/me/my/skep")
    assert shlex.join(recall_command(repo)) in memory_addendum(repo)


def test_addendum_names_the_repo_path_for_store_and_supersede():
    text = memory_addendum(Path("/repos/skep"))
    assert "gortex memory store --index /repos/skep" in text
    assert "--supersedes" in text
    assert "## Memory" in text


def test_addendum_forbids_duplicating_the_repo():
    text = memory_addendum(Path("/repos/skep"))
    assert "Do NOT store what the repo already records" in text


@pytest.fixture
def fake_gortex(tmp_path, monkeypatch):
    def install(script):
        stub = tmp_path / "gortex"
        stub.write_text(f"#!{sys.executable}\n{script}\n")
        stub.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))
        return stub
    return install


async def test_probe_returns_none_when_recall_succeeds(fake_gortex, tmp_path):
    fake_gortex("import sys; sys.exit(0)")
    assert await probe_memory(tmp_path / "repo") is None


async def test_probe_reports_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    reason = await probe_memory(tmp_path / "repo")
    assert reason is not None
    assert "PATH" in reason


async def test_probe_reports_untracked_repo_from_stderr(fake_gortex, tmp_path):
    fake_gortex(
        "import sys; "
        "print('the gortex daemon does not track /repo', file=sys.stderr); "
        "sys.exit(1)"
    )
    reason = await probe_memory(tmp_path / "repo")
    assert reason is not None
    assert "does not track" in reason


async def test_probe_reports_exit_code_when_stderr_is_empty(fake_gortex, tmp_path):
    fake_gortex("import sys; sys.exit(3)")
    reason = await probe_memory(tmp_path / "repo")
    assert reason == "exit 3"


async def test_probe_times_out_on_a_wedged_daemon(fake_gortex, tmp_path):
    fake_gortex("import time; time.sleep(30)")
    reason = await probe_memory(tmp_path / "repo", timeout=0.2)
    assert reason is not None
    assert "did not respond" in reason


async def _done(value):
    """Wrap a value as the coroutine a probe would return."""
    return value


async def test_preflight_returns_the_addendum_when_available():
    pre = MemoryPreflight(probe=lambda repo_path: _done(None))
    text = await pre.addendum_for(Path("/repos/skep"))
    assert text is not None
    assert "/repos/skep" in text


async def test_preflight_returns_none_when_unavailable():
    pre = MemoryPreflight(probe=lambda repo_path: _done("daemon down"))
    assert await pre.addendum_for(Path("/repos/skep")) is None


async def test_preflight_probes_once_per_repo_and_warns_once(caplog):
    calls = []

    def probe(repo_path):
        calls.append(repo_path)
        return _done("daemon down")

    pre = MemoryPreflight(probe=probe)
    with caplog.at_level(logging.WARNING, logger="skep.memory"):
        await pre.addendum_for(Path("/repos/skep"))
        await pre.addendum_for(Path("/repos/skep"))

    assert len(calls) == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "daemon down" in warnings[0].getMessage()


async def test_preflight_probes_each_repo_separately():
    calls = []

    def probe(repo_path):
        calls.append(repo_path)
        return _done(None if repo_path.name == "skep" else "untracked")

    pre = MemoryPreflight(probe=probe)
    assert await pre.addendum_for(Path("/repos/skep")) is not None
    assert await pre.addendum_for(Path("/repos/other")) is None
    assert len(calls) == 2
