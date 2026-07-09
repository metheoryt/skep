# L1 — Agent Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every spawned agent durable, per-repo memory by appending a `gortex memory` system-prompt addendum at spawn — and omit that addendum silently when gortex cannot serve it.

**Architecture:** skep stores nothing. A new `skep.memory` module owns two things: the addendum text and a preflight probe that runs the *exact* `gortex memory recall` invocation the addendum recommends. `Supervisor.spawn` asks the probe for an addendum for the task's **parent repo path** (not the worktree) and, when it gets one, passes it to `AgentProcess` as `--append-system-prompt`. Every failure mode — gortex missing, daemon down, repo untracked, daemon wedged — collapses to "no addendum, one warning, spawn proceeds."

**Tech Stack:** Python 3.13, asyncio, pytest (`asyncio_mode = "auto"`), ruff (`E,F,I,UP,B,ANN`), ty. Package `src/skep/`, tests `tests/`.

## Global Constraints

- **The memory dependency is soft.** No unavailability of gortex may ever fail `Supervisor.spawn`. (spec §3.1)
- **`<repo_path>` in the addendum is always `repos_root/<repo>` — the parent repo, never the worktree.** Agents run in `worktrees_root/<repo>-<tid>`, which the gortex daemon does not track; `--index <parent repo>` is the only verified-working form. (spec §2)
- **The probe runs the real recall command**, built from the same single source as the addendum text. Do not substitute a cheaper check like `gortex daemon status` — the point is that a `--index` rename disables memory loudly instead of handing agents a broken command. (spec §5, test 5)
- **`SKEP_MEMORY_ENABLED`** (default true) forces the addendum off regardless of probe result.
- Source files under `src/` need full type annotations (ruff `ANN`); `tests/**` is exempt (`per-file-ignores`).
- All new source uses `from __future__ import annotations` (repo-wide convention).
- Run the suite with `uv run pytest`. It must stay fully green (218 passed, 1 skipped at plan time).
- **Lint/typecheck tooling:** `ruff` and `ty` are not project dependencies — invoke them as
  `uvx ruff` and `uvx ty`, never `uv run ruff`.
- **The lint baseline is dirty and is NOT yours to clean.** At plan time `uvx ruff check src`
  reports 3 pre-existing `E501`, `uvx ruff check tests` 41 more findings, and `uvx ty check`
  142 diagnostics repo-wide. Therefore **scope every lint gate to the files you created or
  modified** and do not fix unrelated findings — that is out of scope and pollutes the review
  diff. A clean `uvx ruff check <your files>` is the bar; a clean whole repo is not.

### Deviation from the spec, and why

Spec §3.1 says the preflight runs "once at worker startup (not per-spawn)". Tracked-ness is inherently **per repo**, and a worker learns which repos it works on dynamically from the queen — so "not per-spawn" can only mean **cached**. `MemoryPreflight` probes a given repo path once, caches the verdict for the worker's lifetime, and logs its warning exactly once per repo. This satisfies the spec's actual requirement (no per-spawn subprocess, no repeated log spam).

---

## File Structure

| File | Responsibility |
|---|---|
| `src/skep/memory.py` (**new**) | The single source of the `gortex memory recall` argv, the addendum text built from it, the probe, and the per-repo caching `MemoryPreflight`. Nothing else in skep knows the shape of a gortex command. |
| `src/skep/agent.py` (modify) | `AgentProcess` gains an `append_system_prompt` kwarg; `_argv` emits `--append-system-prompt <text>`. Dumb passthrough — no memory knowledge. |
| `src/skep/config.py` (modify) | `WorkerConfig.memory_enabled` ← `SKEP_MEMORY_ENABLED`. |
| `src/skep/supervisor.py` (modify) | `Supervisor` takes an injectable `memory: MemoryProbe | None`. `spawn` gates on `cfg.memory_enabled`, asks for the addendum, and passes it through only when non-`None`. |
| `src/skep/worker/app.py` (modify) | `build_worker` constructs the real `MemoryPreflight()`. |
| `tests/test_memory.py` (**new**) | Addendum content, probe failure modes, cache/warn-once. |
| `tests/test_agent_memory.py` (**new**) | `_argv` with and without the addendum. |
| `tests/test_supervisor_memory.py` (**new**) | Gating, parent-repo-path, soft-dependency. |
| `tests/test_config.py` (modify) | `SKEP_MEMORY_ENABLED` parsing. |
| `README.md` (modify) | Document `SKEP_MEMORY_ENABLED` and the gortex prerequisite. |

**Existing patterns to follow, verbatim:**

- `Supervisor.spawn` builds `agent_kwargs: dict[str, Any]` and **conditionally inserts** optional keys (see the `mcp_url` / `mcp_token` block). Do the same for `append_system_prompt`. This is load-bearing: `tests/test_supervisor.py:70` injects `agent_factory(task_text, cwd, claude_bin, config_dir=None)`, a narrow signature that raises `TypeError` if you unconditionally pass a new kwarg.
- Logging: `logger = logging.getLogger(__name__)` at module scope, as in `src/skep/ws_transport.py:27`.
- Optional collaborators are constructor-injected with a real default supplied in `build_worker`, as with `mailbox_client` / `shim_factory`.

---

## Task 1: The memory module

Everything the rest of the fleet needs — the recall argv, the addendum, the probe, the cache. Self-contained and pure except for the subprocess it launches, which is injectable.

**Files:**
- Create: `src/skep/memory.py`
- Test: `tests/test_memory.py`

**Interfaces:**
- Consumes: nothing (leaf module; stdlib only).
- Produces:
  - `recall_command(repo_path: Path) -> list[str]`
  - `memory_addendum(repo_path: Path) -> str`
  - `async probe_memory(repo_path: Path, timeout: float = PROBE_TIMEOUT) -> str | None` — returns `None` when memory is available, else a short human-readable reason.
  - `class MemoryProbe(Protocol)` with `async def addendum_for(self, repo_path: Path) -> str | None`
  - `class MemoryPreflight` implementing that protocol; `__init__(self, probe: Probe = probe_memory)`.
  - `PROBE_TIMEOUT: float = 10.0`

- [ ] **Step 1: Write the failing tests for the addendum and the single-source guarantee**

Create `tests/test_memory.py`:

```python
import shlex
from pathlib import Path

from skep.memory import memory_addendum, recall_command


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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_memory.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'skep.memory'`

- [ ] **Step 3: Write `recall_command` and `memory_addendum`**

Create `src/skep/memory.py`:

```python
from __future__ import annotations

import asyncio
import logging
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 10.0


def recall_command(repo_path: Path) -> list[str]:
    """The exact invocation the addendum recommends and the preflight probes."""
    return ["gortex", "memory", "recall", "--index", str(repo_path), "--limit", "10"]


def memory_addendum(repo_path: Path) -> str:
    """System-prompt addendum telling an agent it has durable repo memory.

    `repo_path` is the PARENT repo (repos_root/<repo>), never the agent's
    worktree: the gortex daemon tracks the parent, and `--index <parent>` is
    the only form that works from inside a worktree.

    Phrased prescriptively (when to write, not just how) -- prescriptive
    trigger conditions measurably lift correct tool use.
    """
    repo = str(repo_path)
    recall = shlex.join(recall_command(repo_path))
    return f"""## Memory

You have durable memory for this repo, shared with every agent that works on it.

Before starting, recall what is already known:
    {recall}

Store a memory when you learn something that would save the next agent time and
that the repo itself does not already record -- an operational fact (this stack
takes 90s to come up; this test flakes under load), a constraint you discovered
the hard way, or a decision and its reasoning:
    gortex memory store --index {repo} --kind gotcha \\
        --title "<short caption>" --body "<what + why>"

If a memory you find is now wrong, supersede it rather than adding a contradiction:
    gortex memory store --index {repo} --supersedes <id> --body "<corrected>"

Do NOT store what the repo already records -- code structure, git history, CLAUDE.md.
Where the repo holds the fact, reference it instead of copying it.

Scratch notes for this task alone: write a file in your worktree.
"""
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_memory.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Write the failing probe tests**

Append to `tests/test_memory.py`:

```python
import sys

import pytest

from skep.memory import probe_memory


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
```

- [ ] **Step 6: Run them to verify they fail**

Run: `uv run pytest tests/test_memory.py -v -k probe`
Expected: FAIL — `ImportError: cannot import name 'probe_memory'`

- [ ] **Step 7: Implement the probe — total, never raises**

Append to `src/skep/memory.py`:

```python
async def probe_memory(repo_path: Path, timeout: float = PROBE_TIMEOUT) -> str | None:
    """Smoke-check the exact command the addendum recommends.

    Returns None when memory is available, else a short reason. Never raises:
    the memory dependency is soft and must not be able to fail a spawn. A
    wedged daemon is a real failure mode, hence the timeout.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *recall_command(repo_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return "gortex not found on PATH"
    except OSError as exc:
        return f"could not run gortex: {exc}"

    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return f"gortex did not respond within {timeout:g}s"

    if proc.returncode != 0:
        lines = stderr.decode(errors="replace").strip().splitlines()
        return lines[-1].strip() if lines else f"exit {proc.returncode}"
    return None
```

- [ ] **Step 8: Run the probe tests to verify they pass**

Run: `uv run pytest tests/test_memory.py -v`
Expected: PASS (9 passed)

- [ ] **Step 9: Write the failing preflight cache tests**

Append to `tests/test_memory.py`:

```python
import logging

from skep.memory import MemoryPreflight


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
```

Note: the probes above are plain `def`s returning the `_done(...)` coroutine; `MemoryPreflight` awaits whatever the probe returns, so this satisfies the `Probe = Callable[[Path], Awaitable[str | None]]` contract.

- [ ] **Step 10: Run them to verify they fail**

Run: `uv run pytest tests/test_memory.py -v -k preflight`
Expected: FAIL — `ImportError: cannot import name 'MemoryPreflight'`

- [ ] **Step 11: Implement `MemoryProbe` and `MemoryPreflight`**

Append to `src/skep/memory.py`:

```python
Probe = Callable[[Path], Awaitable[str | None]]


class MemoryProbe(Protocol):
    """What Supervisor needs from memory: an addendum, or None."""

    async def addendum_for(self, repo_path: Path) -> str | None: ...


class MemoryPreflight:
    """Probes each repo once, caches the verdict, warns once on unavailability.

    Spec says "preflight once at worker startup"; tracked-ness is per-repo and
    a worker learns its repos dynamically, so once-per-repo-cached is the
    faithful reading. No per-spawn subprocess, no repeated warnings.
    """

    def __init__(self, probe: Probe = probe_memory) -> None:
        self._probe = probe
        self._reasons: dict[Path, str | None] = {}
        self._lock = asyncio.Lock()

    async def addendum_for(self, repo_path: Path) -> str | None:
        async with self._lock:
            if repo_path not in self._reasons:
                reason = await self._probe(repo_path)
                self._reasons[repo_path] = reason
                if reason is not None:
                    logger.warning(
                        "agent memory disabled for %s: %s", repo_path, reason
                    )
        if self._reasons[repo_path] is not None:
            return None
        return memory_addendum(repo_path)
```

- [ ] **Step 12: Run the full module suite**

Run: `uv run pytest tests/test_memory.py -v`
Expected: PASS (13 passed)

If step 13's `ty check` objects to `probe: Probe = probe_memory` — `probe_memory` has an
extra defaulted `timeout` param that the `Probe` alias doesn't name — widen the alias to
`Probe = Callable[..., Awaitable[str | None]]`. Structural checkers usually accept the
narrower form; don't restructure the code if it does.

- [ ] **Step 13: Lint and typecheck**

Run: `uvx ruff check src/skep/memory.py tests/test_memory.py && uvx ruff format --check src/skep/memory.py`
Expected: no errors. If `ruff format --check` complains, run `uvx ruff format src/skep/memory.py` and re-check.

Then check you added no *new* type errors: `uvx ty check 2>&1 | grep memory.py`
Expected: no output. (A bare `uvx ty check` reports 142 pre-existing diagnostics repo-wide —
ignore them, they are not yours.)

- [ ] **Step 14: Commit**

```bash
git add src/skep/memory.py tests/test_memory.py
git commit -m "feat(memory): gortex memory addendum + preflight probe"
```

---

## Task 2: `AgentProcess` emits `--append-system-prompt`

A dumb passthrough. `AgentProcess` knows nothing about gortex.

**Files:**
- Modify: `src/skep/agent.py` (`AgentProcess.__init__`, `AgentProcess._argv`)
- Test: `tests/test_agent_memory.py`

**Interfaces:**
- Consumes: nothing from Task 1 (the caller supplies the text).
- Produces: `AgentProcess(task_text, cwd, claude_bin, config_dir=None, mcp_url=None, mcp_token=None, append_system_prompt: str | None = None)`; when `append_system_prompt` is not `None`, `_argv()` contains `["--append-system-prompt", <text>]` as adjacent elements.

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent_memory.py`:

```python
from skep.agent import AgentProcess


def _argv(**kw):
    return AgentProcess("do it", "/tmp/wt", "claude", **kw)._argv()


def test_no_append_system_prompt_when_absent():
    assert "--append-system-prompt" not in _argv()


def test_append_system_prompt_passed_verbatim():
    argv = _argv(append_system_prompt="## Memory\nrecall stuff\n")
    i = argv.index("--append-system-prompt")
    assert argv[i + 1] == "## Memory\nrecall stuff\n"


def test_append_system_prompt_coexists_with_mcp_config():
    argv = _argv(
        append_system_prompt="## Memory",
        mcp_url="http://127.0.0.1:5000/mcp",
        mcp_token="secret",
    )
    assert "--append-system-prompt" in argv
    assert "--mcp-config" in argv
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_agent_memory.py -v`
Expected: FAIL — `TypeError: AgentProcess.__init__() got an unexpected keyword argument 'append_system_prompt'`

- [ ] **Step 3: Implement**

In `src/skep/agent.py`, add the parameter to `__init__` (after `mcp_token`) and store it:

```python
    def __init__(
        self,
        task_text: str,
        cwd: Path,
        claude_bin: str,
        config_dir: str | None = None,
        mcp_url: str | None = None,
        mcp_token: str | None = None,
        append_system_prompt: str | None = None,
    ) -> None:
        self._task_text = task_text
        self._cwd = cwd
        self._claude_bin = claude_bin
        self._config_dir = config_dir
        self._mcp_url = mcp_url
        self._mcp_token = mcp_token
        self._append_system_prompt = append_system_prompt
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr: bytes = b""
        self._stderr_task: asyncio.Task | None = None
```

Then in `_argv`, insert the flag between the base flags and the `--mcp-config` block:

```python
        if self._append_system_prompt is not None:
            argv += ["--append-system-prompt", self._append_system_prompt]
        if self._mcp_url is not None:
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent_memory.py tests/test_agent_mcp.py tests/test_agent.py -v`
Expected: PASS — the new file's 3 tests plus the existing agent tests, all green.

- [ ] **Step 5: Commit**

```bash
git add src/skep/agent.py tests/test_agent_memory.py
git commit -m "feat(agent): --append-system-prompt passthrough"
```

---

## Task 3: Config flag and Supervisor wiring

The gate lives in `Supervisor` because it is the one unit that sees both inputs — `cfg.memory_enabled` and the probe's verdict — so "enabled=false with an available probe" is expressible as a unit test.

**Files:**
- Modify: `src/skep/config.py` (`WorkerConfig`, `load_worker_config`)
- Modify: `src/skep/supervisor.py` (`Supervisor.__init__`, `Supervisor.spawn`)
- Test: `tests/test_config.py` (append), `tests/test_supervisor_memory.py` (create)

**Interfaces:**
- Consumes: `MemoryProbe` protocol and `memory_addendum` from Task 1; `append_system_prompt` kwarg from Task 2.
- Produces:
  - `WorkerConfig.memory_enabled: bool = True`
  - `Supervisor(config, registry, sink, agent_factory=..., worktree_factory=..., mailbox_client=None, shim_factory=..., memory: MemoryProbe | None = None)`
  - `spawn` passes `append_system_prompt=<addendum>` into `agent_kwargs` **only** when the addendum is non-`None`.

- [ ] **Step 1: Write the failing config test**

Append to `tests/test_config.py`:

```python
def test_memory_enabled_defaults_true():
    env = _worker_env()
    assert load_worker_config(env).memory_enabled is True


def test_memory_enabled_can_be_disabled():
    env = _worker_env() | {"SKEP_MEMORY_ENABLED": "false"}
    assert load_worker_config(env).memory_enabled is False
```

`_worker_env()` is the existing helper at the top of that file. Note that
`test_load_worker_config_parses_fields` compares against a full `WorkerConfig(...)`
literal — it keeps passing because `memory_enabled` defaults to `True` on both sides.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_config.py -v -k memory`
Expected: FAIL — `AttributeError: 'WorkerConfig' object has no attribute 'memory_enabled'`

- [ ] **Step 3: Implement the config flag**

In `src/skep/config.py`, add the field to `WorkerConfig` after `claude_bin`:

```python
    claude_bin: str = "claude"
    memory_enabled: bool = True
```

and to `load_worker_config`, after `claude_bin=...`:

```python
        claude_bin=env.get("SKEP_CLAUDE_BIN", "claude"),
        memory_enabled=_as_bool(env.get("SKEP_MEMORY_ENABLED"), True),
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Write the failing supervisor tests**

Create `tests/test_supervisor_memory.py`:

```python
from pathlib import Path

from skep.config import WorkerConfig
from skep.db import Registry
from skep.supervisor import Supervisor


def _cfg(tmp_path, memory_enabled=True):
    return WorkerConfig(
        host="g16", profile="work", claude_config_dir="/cfg",
        repos_root=tmp_path / "repos", worktrees_root=tmp_path / "wt",
        db_path=":memory:", max_concurrent=8, claude_bin="claude",
        memory_enabled=memory_enabled,
    )


class FakeAgent:
    pid = 123

    async def start(self):
        pass

    async def events(self):
        return
        yield  # pragma: no cover -- makes this an async generator

    async def kill(self):
        pass

    @property
    def returncode(self):
        return 0

    @property
    def stderr_text(self):
        return ""


class RecordingSink:
    """The four methods spawn/run_events actually call on an EventSink."""

    async def task_started(self, *a, **k):
        pass

    async def activity(self, *a, **k):
        pass

    async def milestone(self, *a, **k):
        pass

    async def done(self, *a, **k):
        pass


class StubMemory:
    """MemoryProbe stub: hands back a fixed addendum, records the repo path."""

    def __init__(self, addendum="## Memory\n"):
        self.addendum = addendum
        self.seen = []

    async def addendum_for(self, repo_path):
        self.seen.append(repo_path)
        return self.addendum


class RaisingMemory:
    async def addendum_for(self, repo_path):
        raise RuntimeError("probe exploded")


def _sup(tmp_path, memory, captured, memory_enabled=True):
    def agent_factory(**kwargs):
        captured.update(kwargs)
        return FakeAgent()

    return Supervisor(
        _cfg(tmp_path, memory_enabled=memory_enabled),
        Registry.open(":memory:"),
        RecordingSink(),
        agent_factory=agent_factory,
        worktree_factory=lambda *a, **k: None,
        memory=memory,
    )


async def test_spawn_passes_addendum_for_the_parent_repo(tmp_path):
    captured = {}
    mem = StubMemory("## Memory\nrecall\n")
    await _sup(tmp_path, mem, captured).spawn("skep", "do it")

    assert captured["append_system_prompt"] == "## Memory\nrecall\n"
    # The parent repo, NOT the worktree: gortex tracks the parent only.
    assert mem.seen == [tmp_path / "repos" / "skep"]
    assert captured["cwd"] != tmp_path / "repos" / "skep"


async def test_spawn_omits_addendum_when_memory_unavailable(tmp_path):
    captured = {}
    await _sup(tmp_path, StubMemory(addendum=None), captured).spawn("skep", "do it")
    assert "append_system_prompt" not in captured


async def test_spawn_omits_addendum_when_disabled_even_if_available(tmp_path):
    captured = {}
    mem = StubMemory("## Memory\n")
    sup = _sup(tmp_path, mem, captured, memory_enabled=False)
    await sup.spawn("skep", "do it")

    assert "append_system_prompt" not in captured
    assert mem.seen == []  # disabled means we don't even probe


async def test_spawn_omits_addendum_when_no_memory_probe_configured(tmp_path):
    captured = {}
    await _sup(tmp_path, None, captured).spawn("skep", "do it")
    assert "append_system_prompt" not in captured


async def test_spawn_succeeds_when_the_probe_raises(tmp_path):
    # The memory dependency is soft: nothing it does may fail a spawn.
    captured = {}
    tid = await _sup(tmp_path, RaisingMemory(), captured).spawn("skep", "do it")
    assert tid > 0
    assert "append_system_prompt" not in captured
```

- [ ] **Step 6: Run them to verify they fail**

Run: `uv run pytest tests/test_supervisor_memory.py -v`
Expected: FAIL — `TypeError: Supervisor.__init__() got an unexpected keyword argument 'memory'`

- [ ] **Step 7: Implement the Supervisor wiring**

In `src/skep/supervisor.py`, import the protocol:

```python
from skep.memory import MemoryProbe
```

Add the parameter to `Supervisor.__init__` (last, after `shim_factory`) and store it:

```python
        shim_factory: Callable[..., MailboxShim] = MailboxShim,
        memory: MemoryProbe | None = None,
    ) -> None:
        ...
        self._shim_factory = shim_factory
        self._memory = memory
```

In `spawn`, inside the `try:` block, right after the `agent_kwargs = dict(...)` literal and before the `if self._mailbox_client is not None:` block:

```python
            if self._cfg.memory_enabled and self._memory is not None:
                # Soft dependency: a broken probe must never fail a spawn.
                # `repo_path` (the parent repo) is what gortex tracks -- the
                # agent's worktree is not indexed.
                try:
                    addendum = await self._memory.addendum_for(repo_path)
                except Exception as exc:
                    self._reg.log_audit(tid, "error", f"memory probe failed: {exc}")
                    addendum = None
                if addendum is not None:
                    agent_kwargs["append_system_prompt"] = addendum
```

Note the conditional insert: existing tests inject `agent_factory(task_text, cwd, claude_bin, config_dir=None)`, which raises `TypeError` on an unexpected kwarg. Never pass the key unconditionally.

- [ ] **Step 8: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_supervisor_memory.py -v`
Expected: PASS (5 passed)

`spawn` launches `run_events` as a background task, which drives `FakeAgent.events()` and
the sink's `activity` / `milestone` / `done`. If a test fails on a missing attribute of
`FakeAgent` or `RecordingSink`, widen the fake — it does **not** mean the memory logic is
wrong.

- [ ] **Step 9: Run the whole suite — nothing else may regress**

Run: `uv run pytest`
Expected: all green. The `Supervisor` default `memory=None` keeps every existing supervisor test on the no-addendum path.

- [ ] **Step 10: Lint and typecheck**

Run: `uvx ruff check src/skep/config.py src/skep/supervisor.py tests/test_supervisor_memory.py && uvx ruff format --check src/skep/config.py src/skep/supervisor.py`
Expected: no errors. Do not "fix" pre-existing findings in files you did not touch.

- [ ] **Step 11: Commit**

```bash
git add src/skep/config.py src/skep/supervisor.py tests/test_config.py tests/test_supervisor_memory.py
git commit -m "feat(supervisor): pass gortex memory addendum at spawn, gated by SKEP_MEMORY_ENABLED"
```

---

## Task 4: Wire the real preflight into the worker, and document it

Until this task, `memory=None` everywhere and the feature is dark. This lights it up and tells an operator what it needs.

**Files:**
- Modify: `src/skep/worker/app.py` (`build_worker`)
- Modify: `README.md`
- Test: `tests/test_worker_app.py` (append)

**Interfaces:**
- Consumes: `MemoryPreflight` from Task 1; `Supervisor(..., memory=...)` from Task 3.
- Produces: `build_worker` returns a `Supervisor` whose `_memory` is a real `MemoryPreflight`. No signature change.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_worker_app.py`:

```python
from skep.memory import MemoryPreflight


def test_build_worker_gives_supervisor_a_real_memory_preflight():
    supervisor, _switch, _client = build_worker(_wcfg())
    assert isinstance(supervisor._memory, MemoryPreflight)  # type: ignore[attr-defined]
```

`_wcfg(**kw)` is the existing helper at the top of that file (it builds a `WorkerConfig` from a defaults dict). The `# type: ignore[attr-defined]` comment matches how the file's existing tests reach into `sup._sink`.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_worker_app.py -v -k memory`
Expected: FAIL — `AssertionError` (`supervisor._memory` is `None`)

- [ ] **Step 3: Implement**

In `src/skep/worker/app.py`, add the import and pass the preflight:

```python
from skep.memory import MemoryPreflight
```

```python
    supervisor = Supervisor(
        wcfg,
        registry,
        switch,
        mailbox_client=mailbox_switch,
        memory=MemoryPreflight(),
    )
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/test_worker_app.py -v`
Expected: PASS

- [ ] **Step 5: Document the prerequisite and the kill switch**

In `README.md`, inside the fenced config block in Setup step 4, add below the existing `SKEP_CLAUDE_BIN` comment:

```sh
   # optional: SKEP_MEMORY_ENABLED=false   # turn off the agent-memory addendum
```

And add a new section after `## Setup`:

```markdown
## Agent memory

Agents get durable, per-repo memory through the machine's
[gortex](https://github.com/gortexhq/gortex) daemon — skep stores nothing. At
spawn, each agent's system prompt gains a short addendum telling it to
`gortex memory recall --index <repo>` before starting and to store operational
facts it learns.

This requires a running gortex daemon on the worker host that **tracks each repo
agents work in**. The worker smoke-checks the exact recall command once per repo;
if gortex is missing, the daemon is down or wedged, or the repo is untracked, the
addendum is omitted, a warning is logged, and agents run normally without memory.
Set `SKEP_MEMORY_ENABLED=false` to omit it unconditionally.

Memory is scoped to the repo's gortex workspace and is shared by every agent that
works on that repo. Profiles are isolated only because personal and work profiles
live on separate hosts with separate daemons — co-locating them would share a
repo's memory across profiles. See
`docs/superpowers/specs/2026-07-09-l1-memory-substrate-design.md` §4.1.
```

- [ ] **Step 6: Run the full suite and the linters**

Run: `uv run pytest && uvx ruff check src/skep/worker/app.py tests/test_worker_app.py && uvx ruff format --check src/skep/worker/app.py`
Expected: the full suite green; no lint errors in the files this task touched.

- [ ] **Step 7: Commit**

```bash
git add src/skep/worker/app.py tests/test_worker_app.py README.md
git commit -m "feat(worker): enable agent memory preflight; document gortex prerequisite"
```

---

## Manual verification (once, on a real worker box)

Automated tests inject the probe and never touch gortex. Do this once by hand — the spec's §5 risk is that `--index` churns on a pre-1.0 tool.

- [ ] Confirm the daemon is up and tracks the repo: `gortex daemon status`
- [ ] Confirm the probed command works: `gortex memory recall --index $HOME/my/skep --limit 10` → exit 0
- [ ] Spawn a real agent at a repo the daemon tracks; confirm no `agent memory disabled` warning in the worker log.
- [ ] Stop the daemon (`gortex daemon stop`), spawn again; confirm exactly one `agent memory disabled for … ` warning and that the task still runs to completion.
- [ ] Restart the daemon.

---

## Spec coverage

| Spec requirement | Task |
|---|---|
| §3 `--append-system-prompt <addendum>` at spawn | 2, 3 |
| §3 addendum text, prescriptive triggers | 1 (step 3) |
| §3 `<repo_path>` is `repos_root/<repo>` | 3 (step 5 test asserts parent, not worktree) |
| §3.1 preflight, not per-spawn | 1 (`MemoryPreflight` cache) |
| §3.1 omit entirely + one visible warning | 1 (warn-once test), 3 (conditional insert) |
| §3.1 `SKEP_MEMORY_ENABLED` (default true) | 3 |
| §5 flag churn disables loudly | 1 (probe runs the real recall command) |
| §6 test 1 — addendum present with correct path | 3 |
| §6 test 2 — omitted on down/missing/untracked + warning | 1, 3 |
| §6 test 3 — `SKEP_MEMORY_ENABLED=false` overrides | 3 |
| §6 test 4 — spawn succeeds in every unavailable case | 3 (`test_spawn_succeeds_when_the_probe_raises`, `..._when_memory_unavailable`) |
| §6 test 5 — addendum command == probed command | 1 (`test_addendum_embeds_the_exact_probed_recall_command`) |
| §4.1 profile-isolation assumption is documented | 4 (README) |

Not implemented, by design: §7's queen-hosted store, consolidation, and vectors are explicitly deferred. Per-task scratch needs no code (spec §1).
