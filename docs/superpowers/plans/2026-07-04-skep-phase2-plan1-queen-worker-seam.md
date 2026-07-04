# skep Phase 2 — Plan 1: Queen/Worker split behind the transport seam

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the Phase-1 single process into a **queen** role (owns Telegram, topics, formatting, aggregation, bookkeeping) and a **worker** role (owns agents + local registry), communicating through an abstract `EventSink`/`CommandHandler` seam with an **in-memory transport** — same end-to-end behavior as Phase 1, now split, with `host`/`profile` identity, a capacity cap, profile isolation, and `ref`-based task addressing.

**Architecture:** The worker's `Supervisor` no longer touches Telegram; it emits domain events (`task_started`/`activity`/`milestone`/`done`) into an `EventSink`. The queen implements the receiving side (`QueenInbox`), rendering events into per-task Telegram topics via a small bookkeeping SQLite (`ref → host, profile, local_task_id, topic_id, activity_msg_id, …`). Commands flow the other way through a `CommandHandler` the worker implements (`spawn`/`kill`/`panic`). Plan 1 wires one queen + one worker in a single process over an in-memory transport; Plan 2 replaces that transport with WebSocket and splits the entrypoints.

**Tech Stack:** Python 3.13, asyncio, aiogram 3.x (Telegram, long-polling), stdlib `sqlite3`, `uv`, `pytest` + `pytest-asyncio`.

## Global Constraints

- Python **3.13**; asyncio throughout.
- Telegram library **aiogram 3.x**, long-polling; no webhook.
- Persistence: stdlib **`sqlite3`**.
- **Auth (unchanged):** every Telegram update rejected unless `from_user.id == owner_id`, enforced by a `dp.update.outer_middleware` PLUS per-handler `F.func(owner_only)`.
- **All outbound Telegram text is MarkdownV2.** Dynamic values escaped with `escape_md`, or sent with `parse_mode=None`. **New rule:** escaping happens **on the queen only** — the worker emits plain (unescaped) semantic text; the queen calls `escape_md` before sending.
- **`host` and `profile` are separate fields everywhere** — never concatenated into a parseable id. By-id commands use the queen's opaque global `ref`.
- **Agent runtime unchanged from Phase 1:** `claude -p "<task>" --output-format stream-json --verbose`, `stdin=DEVNULL`. `--input-format`/stdin stay deferred to Phase 3.
- **Profile isolation:** every spawned agent gets `CLAUDE_CONFIG_DIR` set to the worker's `claude_config_dir` (when configured).
- Plan 1 is **in-memory transport, single process** — no networking, no new runtime deps. WebSocket, auth, mDNS, heartbeat, and the split entrypoints are **Plan 2**.

## File Structure

```
src/skep/
  config.py            # MODIFY: split Config -> WorkerConfig + QueenConfig
  transport.py         # CREATE: EventSink / CommandHandler / QueenInbox + InMemoryEventSink
  formatting.py        # MODIFY: activity_line/milestone_message -> plain (no escape); escape_md stays
  agent.py             # MODIFY: AgentProcess gains config_dir -> CLAUDE_CONFIG_DIR env injection
  supervisor.py        # MODIFY: Supervisor(config, registry, sink); emits EventSink; capacity; CommandHandler
  queen/
    __init__.py        # CREATE
    bookkeeping.py     # CREATE: Bookkeeping SQLite (ref mapping + display status)
    telegram_sink.py   # CREATE: QueenSink (QueenInbox impl) -> topics/messages via Gateway
    router.py          # CREATE: QueenRouter — worker registry + /spawn /ls /kill /panic
  app.py               # MODIFY: interim single-process wiring (queen+worker over in-memory) + handlers
tests/
  test_config.py       # MODIFY
  test_transport.py    # CREATE
  test_formatting.py   # MODIFY
  test_agent.py        # MODIFY (add env-injection test)
  test_supervisor.py   # MODIFY
  test_bookkeeping.py  # CREATE
  test_telegram_sink.py# CREATE
  test_router.py       # CREATE
  test_integration.py  # MODIFY
```

`telegram_gw.py`, `stream.py`, `db.py` are unchanged in Plan 1.

---

## Task 1: Split config into WorkerConfig + QueenConfig

**Files:**
- Modify: `src/skep/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `WorkerConfig` (frozen): `host: str`, `profile: str`, `claude_config_dir: str | None`, `repos_root: Path`, `worktrees_root: Path`, `db_path: str`, `max_concurrent: int = 8`, `claude_bin: str = "claude"`.
  - `QueenConfig` (frozen): `bot_token: str`, `owner_id: int`, `group_chat_id: int`, `bookkeeping_db: str = "queen.sqlite"`.
  - `load_worker_config(env: Mapping[str, str]) -> WorkerConfig`, `load_queen_config(env: Mapping[str, str]) -> QueenConfig`.

- [ ] **Step 1: Replace `tests/test_config.py`**

```python
from pathlib import Path

import pytest

from skep.config import QueenConfig, WorkerConfig, load_queen_config, load_worker_config


def _worker_env():
    return {
        "SKEP_HOST": "g16",
        "SKEP_PROFILE": "work",
        "SKEP_CLAUDE_CONFIG_DIR": "/home/me/.claude-work",
        "SKEP_REPOS_ROOT": "/home/me/gh",
        "SKEP_WORKTREES_ROOT": "/home/me/.skep/wt",
        "SKEP_DB": "/home/me/.skep/work.sqlite",
    }


def _queen_env():
    return {
        "SKEP_BOT_TOKEN": "tok",
        "SKEP_OWNER_ID": "42",
        "SKEP_GROUP_CHAT_ID": "-1001",
    }


def test_load_worker_config_parses_fields():
    cfg = load_worker_config(_worker_env())
    assert cfg == WorkerConfig(
        host="g16",
        profile="work",
        claude_config_dir="/home/me/.claude-work",
        repos_root=Path("/home/me/gh"),
        worktrees_root=Path("/home/me/.skep/wt"),
        db_path="/home/me/.skep/work.sqlite",
        max_concurrent=8,
        claude_bin="claude",
    )


def test_worker_host_defaults_to_hostname(monkeypatch):
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "boxy")
    env = _worker_env()
    del env["SKEP_HOST"]
    assert load_worker_config(env).host == "boxy"


def test_worker_profile_defaults_to_default():
    env = _worker_env()
    del env["SKEP_PROFILE"]
    assert load_worker_config(env).profile == "default"


def test_worker_claude_config_dir_optional():
    env = _worker_env()
    del env["SKEP_CLAUDE_CONFIG_DIR"]
    assert load_worker_config(env).claude_config_dir is None


def test_worker_max_concurrent_override():
    env = _worker_env() | {"SKEP_MAX_CONCURRENT": "3"}
    assert load_worker_config(env).max_concurrent == 3


def test_load_queen_config_parses_fields():
    cfg = load_queen_config(_queen_env())
    assert cfg == QueenConfig(bot_token="tok", owner_id=42, group_chat_id=-1001,
                              bookkeeping_db="queen.sqlite")


def test_queen_missing_token_raises():
    env = _queen_env()
    del env["SKEP_BOT_TOKEN"]
    with pytest.raises(KeyError):
        load_queen_config(env)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'WorkerConfig'`.

- [ ] **Step 3: Replace `src/skep/config.py`**

```python
from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class WorkerConfig:
    host: str
    profile: str
    claude_config_dir: str | None
    repos_root: Path
    worktrees_root: Path
    db_path: str
    max_concurrent: int = 8
    claude_bin: str = "claude"


@dataclass(frozen=True)
class QueenConfig:
    bot_token: str
    owner_id: int
    group_chat_id: int
    bookkeeping_db: str = "queen.sqlite"


def load_worker_config(env: Mapping[str, str]) -> WorkerConfig:
    return WorkerConfig(
        host=env.get("SKEP_HOST") or socket.gethostname(),
        profile=env.get("SKEP_PROFILE", "default"),
        claude_config_dir=env.get("SKEP_CLAUDE_CONFIG_DIR"),
        repos_root=Path(env["SKEP_REPOS_ROOT"]),
        worktrees_root=Path(env["SKEP_WORKTREES_ROOT"]),
        db_path=env["SKEP_DB"],
        max_concurrent=int(env.get("SKEP_MAX_CONCURRENT", "8")),
        claude_bin=env.get("SKEP_CLAUDE_BIN", "claude"),
    )


def load_queen_config(env: Mapping[str, str]) -> QueenConfig:
    return QueenConfig(
        bot_token=env["SKEP_BOT_TOKEN"],
        owner_id=int(env["SKEP_OWNER_ID"]),
        group_chat_id=int(env["SKEP_GROUP_CHAT_ID"]),
        bookkeeping_db=env.get("SKEP_QUEEN_DB", "queen.sqlite"),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add src/skep/config.py tests/test_config.py
git commit -m "refactor: split Config into WorkerConfig + QueenConfig"
```

---

## Task 2: Transport seam (interfaces + in-memory link)

**Files:**
- Create: `src/skep/transport.py`
- Test: `tests/test_transport.py`

**Interfaces:**
- Produces:
  - `EventSink` (Protocol, worker→queen): `async task_started(local_id: int, repo: str, title: str)`, `async activity(local_id: int, line: str)`, `async milestone(local_id: int, text: str)`, `async done(local_id: int, status: str, summary: str)`.
  - `CommandHandler` (Protocol, queen→worker): `async spawn(repo: str, task: str)`, `async kill(local_id: int)`, `async panic()`.
  - `QueenInbox` (Protocol, the queen's event receiver): `async on_task_started(host, profile, local_id, repo, title)`, `async on_activity(host, profile, local_id, line)`, `async on_milestone(host, profile, local_id, text)`, `async on_done(host, profile, local_id, status, summary)`.
  - `InMemoryEventSink(inbox: QueenInbox, host: str, profile: str)` — an `EventSink` that forwards each call to `inbox`, stamping `host`/`profile`.

- [ ] **Step 1: Write the failing test** — `tests/test_transport.py`

```python
from skep.transport import InMemoryEventSink


class RecordingInbox:
    def __init__(self):
        self.calls = []

    async def on_task_started(self, host, profile, local_id, repo, title):
        self.calls.append(("started", host, profile, local_id, repo, title))

    async def on_activity(self, host, profile, local_id, line):
        self.calls.append(("activity", host, profile, local_id, line))

    async def on_milestone(self, host, profile, local_id, text):
        self.calls.append(("milestone", host, profile, local_id, text))

    async def on_done(self, host, profile, local_id, status, summary):
        self.calls.append(("done", host, profile, local_id, status, summary))


async def test_in_memory_sink_stamps_identity_and_forwards():
    inbox = RecordingInbox()
    sink = InMemoryEventSink(inbox, host="g16", profile="work")

    await sink.task_started(5, "nix", "clean nvidia")
    await sink.activity(5, "🔧 edit_file")
    await sink.milestone(5, "✅ Done: finished")
    await sink.done(5, "done", "finished")

    assert inbox.calls == [
        ("started", "g16", "work", 5, "nix", "clean nvidia"),
        ("activity", "g16", "work", 5, "🔧 edit_file"),
        ("milestone", "g16", "work", 5, "✅ Done: finished"),
        ("done", "g16", "work", 5, "done", "finished"),
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_transport.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'skep.transport'`.

- [ ] **Step 3: Write `src/skep/transport.py`**

```python
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EventSink(Protocol):
    """Worker -> queen. The worker's Supervisor calls these; identity is implicit."""
    async def task_started(self, local_id: int, repo: str, title: str) -> None: ...
    async def activity(self, local_id: int, line: str) -> None: ...
    async def milestone(self, local_id: int, text: str) -> None: ...
    async def done(self, local_id: int, status: str, summary: str) -> None: ...


@runtime_checkable
class CommandHandler(Protocol):
    """Queen -> worker. The worker (Supervisor) implements these."""
    async def spawn(self, repo: str, task: str) -> None: ...
    async def kill(self, local_id: int) -> None: ...
    async def panic(self) -> None: ...


@runtime_checkable
class QueenInbox(Protocol):
    """The queen's receiving side. Each call carries the sender (host, profile)."""
    async def on_task_started(self, host: str, profile: str, local_id: int,
                              repo: str, title: str) -> None: ...
    async def on_activity(self, host: str, profile: str, local_id: int,
                          line: str) -> None: ...
    async def on_milestone(self, host: str, profile: str, local_id: int,
                           text: str) -> None: ...
    async def on_done(self, host: str, profile: str, local_id: int,
                      status: str, summary: str) -> None: ...


class InMemoryEventSink:
    """An EventSink bound to one worker's (host, profile) that forwards to a QueenInbox.

    Plan 2 adds a WebSocket EventSink implementing the same interface.
    """

    def __init__(self, inbox: QueenInbox, host: str, profile: str):
        self._inbox = inbox
        self._host = host
        self._profile = profile

    async def task_started(self, local_id: int, repo: str, title: str) -> None:
        await self._inbox.on_task_started(self._host, self._profile, local_id, repo, title)

    async def activity(self, local_id: int, line: str) -> None:
        await self._inbox.on_activity(self._host, self._profile, local_id, line)

    async def milestone(self, local_id: int, text: str) -> None:
        await self._inbox.on_milestone(self._host, self._profile, local_id, text)

    async def done(self, local_id: int, status: str, summary: str) -> None:
        await self._inbox.on_done(self._host, self._profile, local_id, status, summary)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_transport.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add src/skep/transport.py tests/test_transport.py
git commit -m "feat: transport seam (EventSink/CommandHandler/QueenInbox) + in-memory link"
```

---

## Task 3: Move escaping to the queen — plain formatting descriptors

**Files:**
- Modify: `src/skep/formatting.py`
- Test: `tests/test_formatting.py`

**Interfaces:**
- Consumes: `Event` from `skep.stream`.
- Produces (changed semantics): `activity_line(event) -> str | None` and `milestone_message(event) -> str | None` now return **plain, unescaped** text. `escape_md(text) -> str` unchanged (the queen applies it).

- [ ] **Step 1: Replace `tests/test_formatting.py`**

```python
from skep.formatting import activity_line, escape_md, milestone_message
from skep.stream import Event


def test_escape_md_escapes_reserved_chars():
    assert escape_md("a_b*c[d]") == r"a\_b\*c\[d\]"
    assert escape_md("v1.2-3") == r"v1\.2\-3"


def test_activity_line_for_assistant_text_is_plain():
    ev = Event(kind="assistant_text", text="Refactoring the module")
    assert activity_line(ev) == "💬 Refactoring the module"


def test_activity_line_for_tool_use_is_plain():
    ev = Event(kind="tool_use", tool_name="edit_file")
    assert activity_line(ev) == "🔧 edit_file"


def test_activity_line_none_for_tool_result():
    assert activity_line(Event(kind="tool_result")) is None


def test_milestone_for_successful_result_is_plain():
    ev = Event(kind="result", text="All done", is_error=False)
    assert milestone_message(ev) == "✅ Done: All done"


def test_milestone_for_error_result_is_plain():
    ev = Event(kind="result", text="boom", is_error=True)
    assert milestone_message(ev) == "❌ Failed: boom"


def test_milestone_none_for_assistant_text():
    assert milestone_message(Event(kind="assistant_text", text="x")) is None


def test_activity_line_truncates_long_text():
    ev = Event(kind="assistant_text", text="x" * 300)
    line = activity_line(ev)
    assert len(line) <= 200
    assert line.endswith("…")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_formatting.py -v`
Expected: FAIL — `test_activity_line_for_tool_use_is_plain` expects `"🔧 edit_file"` but current code escapes to `"🔧 edit\\_file"`.

- [ ] **Step 3: Edit `src/skep/formatting.py`** — drop `escape_md` from the two descriptors (keep `escape_md` itself and `_truncate`):

```python
def activity_line(event: Event) -> str | None:
    if event.kind == "assistant_text":
        return _truncate("💬 " + event.text)
    if event.kind == "tool_use":
        return _truncate("🔧 " + event.tool_name)
    return None


def milestone_message(event: Event) -> str | None:
    if event.kind != "result":
        return None
    if event.is_error:
        return "❌ Failed: " + event.text
    return "✅ Done: " + event.text
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_formatting.py -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add src/skep/formatting.py tests/test_formatting.py
git commit -m "refactor: activity/milestone descriptors are plain; escaping moves to queen"
```

---

## Task 4: Profile isolation — CLAUDE_CONFIG_DIR env injection

**Files:**
- Modify: `src/skep/agent.py`
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `AgentProcess(task_text: str, cwd: Path, claude_bin: str, config_dir: str | None = None)`. When `config_dir` is set, the spawned process env has `CLAUDE_CONFIG_DIR=config_dir`. Otherwise env is inherited unchanged. New helper `_agent_env(config_dir: str | None) -> dict[str, str]`.

- [ ] **Step 1: Add failing tests to `tests/test_agent.py`** (append):

```python
from skep.agent import _agent_env


def test_agent_env_injects_config_dir(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    env = _agent_env("/home/me/.claude-work")
    assert env["CLAUDE_CONFIG_DIR"] == "/home/me/.claude-work"
    assert env["PATH"] == "/usr/bin"  # base env preserved


def test_agent_env_none_leaves_config_dir_unset(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    env = _agent_env(None)
    assert "CLAUDE_CONFIG_DIR" not in env
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent.py::test_agent_env_injects_config_dir -v`
Expected: FAIL — `ImportError: cannot import name '_agent_env'`.

- [ ] **Step 3: Edit `src/skep/agent.py`**

Add the import and helper near the top (after `from skep.stream import ...`):

```python
import os


def _agent_env(config_dir: str | None) -> dict[str, str]:
    env = dict(os.environ)
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    return env
```

Change `AgentProcess.__init__` to accept `config_dir`:

```python
    def __init__(self, task_text: str, cwd: Path, claude_bin: str,
                 config_dir: str | None = None):
        self._task_text = task_text
        self._cwd = cwd
        self._claude_bin = claude_bin
        self._config_dir = config_dir
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr: bytes = b""
        self._stderr_task: asyncio.Task | None = None
```

In `start()`, pass `env=` to `create_subprocess_exec`:

```python
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv(),
            cwd=str(self._cwd),
            env=_agent_env(self._config_dir),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent.py -v`
Expected: PASS (all — the two new tests plus the existing 3).

- [ ] **Step 5: Commit**

```bash
git add src/skep/agent.py tests/test_agent.py
git commit -m "feat: agent CLAUDE_CONFIG_DIR injection for per-worker profile isolation"
```

---

## Task 5: Refactor Supervisor to emit EventSink + capacity cap

**Files:**
- Modify: `src/skep/supervisor.py`
- Test: `tests/test_supervisor.py`

**Interfaces:**
- Consumes: `WorkerConfig`, `Registry`, `EventSink`, `AgentProcess`, `create_worktree`, `activity_line`, `milestone_message`.
- Produces:
  - `class CapacityError(Exception)`.
  - `class Supervisor(config: WorkerConfig, registry: Registry, sink: EventSink, agent_factory=AgentProcess, worktree_factory=create_worktree)` implementing `CommandHandler`:
    - `async spawn(repo: str, task: str) -> int` — capacity check against `config.max_concurrent`; creates worktree; records task; starts agent with `config_dir=config.claude_config_dir`; emits `sink.task_started(tid, repo, title)`; launches `run_events`. Raises `CapacityError` when full.
    - `async run_events(task_id, agent)` — emits `sink.activity`/`sink.milestone`/`sink.done`; updates registry; no Telegram.
    - `async kill(task_id) -> bool`, `async panic() -> int`, `def list_active() -> list[Task]`.

- [ ] **Step 1: Replace `tests/test_supervisor.py`**

```python
from pathlib import Path

import pytest

from skep.config import WorkerConfig
from skep.db import Registry
from skep.stream import Event
from skep.supervisor import CapacityError, Supervisor


def _cfg(tmp_path, max_concurrent=8):
    return WorkerConfig(
        host="g16", profile="work", claude_config_dir="/cfg",
        repos_root=tmp_path / "repos", worktrees_root=tmp_path / "wt",
        db_path=":memory:", max_concurrent=max_concurrent, claude_bin="claude",
    )


class FakeAgent:
    def __init__(self, events, config_dir=None):
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
    def __init__(self):
        self.events = []

    async def task_started(self, local_id, repo, title):
        self.events.append(("started", local_id, repo, title))

    async def activity(self, local_id, line):
        self.events.append(("activity", local_id, line))

    async def milestone(self, local_id, text):
        self.events.append(("milestone", local_id, text))

    async def done(self, local_id, status, summary):
        self.events.append(("done", local_id, status, summary))


async def test_spawn_records_task_and_emits_task_started(tmp_path):
    cfg = _cfg(tmp_path)
    (cfg.repos_root / "nix").mkdir(parents=True)
    reg = Registry.open(":memory:")
    sink = RecordingSink()
    captured = {}

    def agent_factory(task_text, cwd, claude_bin, config_dir=None):
        captured["config_dir"] = config_dir
        captured["cwd"] = cwd
        return FakeAgent([Event(kind="system", session_id="s9")])

    sup = Supervisor(cfg, reg, sink, agent_factory=agent_factory,
                     worktree_factory=lambda *a, **k: None)
    tid = await sup.spawn("nix", "clean nvidia")

    task = reg.get_task(tid)
    assert task.repo == "nix"
    assert captured["config_dir"] == "/cfg"        # profile isolation wired through
    assert ("started", tid, "nix", "clean nvidia") in sink.events


async def test_run_events_emits_activity_milestone_done(tmp_path):
    cfg = _cfg(tmp_path)
    reg = Registry.open(":memory:")
    sink = RecordingSink()
    events = [
        Event(kind="system", session_id="s9"),
        Event(kind="assistant_text", text="hi"),
        Event(kind="tool_use", tool_name="edit_file"),
        Event(kind="result", text="finished", is_error=False),
    ]
    agent = FakeAgent(events)
    sup = Supervisor(cfg, reg, sink, agent_factory=lambda **k: agent,
                     worktree_factory=lambda *a, **k: None)
    tid = reg.add_task("nix", "t", str(tmp_path / "wt"))

    await sup.run_events(tid, agent)

    task = reg.get_task(tid)
    assert task.status == "done"
    assert task.session_id == "s9"
    kinds = [e[0] for e in sink.events]
    assert "activity" in kinds and "milestone" in kinds and "done" in kinds
    assert ("done", tid, "done", "finished") in sink.events


async def test_spawn_rejects_over_capacity(tmp_path):
    cfg = _cfg(tmp_path, max_concurrent=1)
    (cfg.repos_root / "nix").mkdir(parents=True)
    reg = Registry.open(":memory:")
    sup = Supervisor(cfg, reg, RecordingSink(),
                     agent_factory=lambda **k: FakeAgent([]),
                     worktree_factory=lambda *a, **k: None)
    await sup.spawn("nix", "one")  # fills the single slot (agent never finishes here)
    with pytest.raises(CapacityError):
        await sup.spawn("nix", "two")


async def test_kill_unknown_returns_false(tmp_path):
    sup = Supervisor(_cfg(tmp_path), Registry.open(":memory:"), RecordingSink())
    assert await sup.kill(999) is False


async def test_panic_kills_all_active(tmp_path):
    cfg = _cfg(tmp_path)
    reg = Registry.open(":memory:")
    sup = Supervisor(cfg, reg, RecordingSink())
    a1, a2 = FakeAgent([]), FakeAgent([])
    t1 = reg.add_task("r", "a", "/wt/a"); reg.update(t1, status="running")
    t2 = reg.add_task("r", "b", "/wt/b"); reg.update(t2, status="running")
    sup._agents = {t1: a1, t2: a2}
    n = await sup.panic()
    assert n == 2
    assert a1.killed and a2.killed
```

> Note: `test_spawn_rejects_over_capacity` relies on the spawned agent staying in `_agents` (its `run_events` task hasn't drained). `FakeAgent([])` yields no events, so `run_events` may complete and remove it — to keep the slot filled deterministically the implementation checks capacity **before** inserting, and the test's first `spawn` inserts synchronously before `run_events` is scheduled. The capacity check counts `self._agents` at call time.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_supervisor.py -v`
Expected: FAIL — `ImportError: cannot import name 'CapacityError'`.

- [ ] **Step 3: Replace `src/skep/supervisor.py`**

```python
from __future__ import annotations

import asyncio
import re

from skep.agent import AgentProcess, create_worktree
from skep.config import WorkerConfig
from skep.db import Registry, Task
from skep.formatting import activity_line, milestone_message
from skep.transport import EventSink


class CapacityError(Exception):
    """Raised when a worker is at max_concurrent and cannot accept a new task."""


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:24] or "task"


class Supervisor:
    def __init__(self, config: WorkerConfig, registry: Registry, sink: EventSink,
                 agent_factory=AgentProcess, worktree_factory=create_worktree):
        self._cfg = config
        self._reg = registry
        self._sink = sink
        self._agent_factory = agent_factory
        self._worktree_factory = worktree_factory
        self._agents: dict[int, AgentProcess] = {}
        self._tasks: set[asyncio.Task] = set()

    def list_active(self) -> list[Task]:
        return self._reg.list_active()

    async def spawn(self, repo: str, task: str) -> int:
        if len(self._agents) >= self._cfg.max_concurrent:
            raise CapacityError(
                f"at capacity ({self._cfg.max_concurrent} running)"
            )
        repo_path = self._cfg.repos_root / repo
        tid = self._reg.add_task(repo, task, "", mode="native")
        branch = f"skep/{_slug(task)}-{tid}"
        worktree_path = self._cfg.worktrees_root / f"{repo}-{tid}"
        self._reg.update(tid, worktree_path=str(worktree_path))

        try:
            self._worktree_factory(repo_path, worktree_path, branch)
            self._reg.log_audit(tid, "spawn", f"{repo}: {task}")
            agent = self._agent_factory(
                task_text=task, cwd=worktree_path,
                claude_bin=self._cfg.claude_bin,
                config_dir=self._cfg.claude_config_dir,
            )
            await agent.start()
            self._agents[tid] = agent
            self._reg.update(tid, status="running", pid=agent.pid)
        except Exception as exc:
            self._reg.update(tid, status="failed")
            self._reg.log_audit(tid, "error", f"spawn failed: {exc}")
            raise

        await self._sink.task_started(tid, repo, task)
        t = asyncio.create_task(self.run_events(tid, agent))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return tid

    async def run_events(self, task_id: int, agent: AgentProcess) -> None:
        activity_started = False
        terminal = "done"
        saw_result = False
        summary = ""
        try:
            async for ev in agent.events():
                if ev.kind == "system" and ev.session_id:
                    self._reg.update(task_id, session_id=ev.session_id)
                if ev.kind == "result":
                    saw_result = True
                    summary = ev.text
                    self._reg.update(
                        task_id,
                        session_id=ev.session_id
                        or self._reg.get_task(task_id).session_id,
                    )
                    terminal = "failed" if ev.is_error else "done"

                line = activity_line(ev)
                if line is not None:
                    await self._sink.activity(task_id, line)
                    activity_started = True

                milestone = milestone_message(ev)
                if milestone is not None:
                    await self._sink.milestone(task_id, milestone)

            if not saw_result and self._reg.get_task(task_id).status != "killed":
                terminal = "failed"
                summary = f"agent exited without result (rc={agent.returncode})"
                self._reg.log_audit(
                    task_id, "error",
                    f"{summary}: {agent.stderr_text[-500:]}",
                )
        except Exception as exc:
            terminal = "failed"
            summary = f"run_events crashed: {exc}"
            self._reg.log_audit(task_id, "error", summary)
        finally:
            if self._reg.get_task(task_id).status == "killed":
                terminal = "killed"
            self._reg.update(task_id, status=terminal)
            self._agents.pop(task_id, None)
            _ = activity_started  # activity presence is tracked by the queen
            await self._sink.done(task_id, terminal, summary)

    async def kill(self, task_id: int) -> bool:
        agent = self._agents.get(task_id)
        if agent is None:
            return False
        self._reg.update(task_id, status="killed")
        self._reg.log_audit(task_id, "kill", "manual kill")
        await agent.kill()
        return True

    async def panic(self) -> int:
        ids = list(self._agents.keys())
        for tid in ids:
            await self.kill(tid)
        self._reg.log_audit(None, "panic", f"killed {len(ids)} agents")
        return len(ids)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_supervisor.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/skep/supervisor.py tests/test_supervisor.py
git commit -m "refactor: Supervisor emits EventSink domain events + capacity cap"
```

---

## Task 6: Queen bookkeeping store

**Files:**
- Create: `src/skep/queen/__init__.py`, `src/skep/queen/bookkeeping.py`
- Test: `tests/test_bookkeeping.py`

**Interfaces:**
- Produces:
  - `Entry` dataclass: `ref: int`, `host: str`, `profile: str`, `local_id: int`, `repo: str`, `title: str`, `topic_id: int`, `activity_msg_id: int | None`, `status: str`.
  - `Bookkeeping` with `open(path) -> Bookkeeping`, `add(host, profile, local_id, repo, title, topic_id) -> int` (returns `ref`), `by_worker_task(host, profile, local_id) -> Entry | None`, `get(ref) -> Entry | None`, `set_activity_msg(ref, msg_id) -> None`, `set_status(ref, status) -> None`, `list_active() -> list[Entry]`, `close()`. Active = status in (`spawning`, `running`).

- [ ] **Step 1: Write the failing test** — `tests/test_bookkeeping.py`

```python
from skep.queen.bookkeeping import Bookkeeping


def test_add_and_lookup_by_worker_task():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("g16", "work", 5, "nix", "clean nvidia", topic_id=555)
    e = bk.by_worker_task("g16", "work", 5)
    assert e.ref == ref
    assert (e.host, e.profile, e.local_id, e.repo, e.topic_id) == (
        "g16", "work", 5, "nix", 555,
    )
    assert e.status == "running"
    assert e.activity_msg_id is None


def test_get_by_ref():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("g16", "work", 5, "nix", "t", topic_id=1)
    assert bk.get(ref).ref == ref
    assert bk.get(999) is None


def test_set_activity_msg_and_status():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("g16", "work", 5, "nix", "t", topic_id=1)
    bk.set_activity_msg(ref, 42)
    bk.set_status(ref, "done")
    e = bk.get(ref)
    assert e.activity_msg_id == 42
    assert e.status == "done"


def test_list_active_excludes_terminal():
    bk = Bookkeeping.open(":memory:")
    a = bk.add("g16", "work", 1, "r", "a", topic_id=1)
    b = bk.add("g16", "work", 2, "r", "b", topic_id=2)
    bk.set_status(b, "done")
    assert [e.ref for e in bk.list_active()] == [a]


def test_worker_task_pairs_are_distinct_per_host_profile():
    bk = Bookkeeping.open(":memory:")
    r1 = bk.add("g16", "work", 5, "r", "a", topic_id=1)
    r2 = bk.add("g16", "personal", 5, "r", "b", topic_id=2)
    assert r1 != r2
    assert bk.by_worker_task("g16", "personal", 5).ref == r2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_bookkeeping.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'skep.queen'`.

- [ ] **Step 3: Create `src/skep/queen/__init__.py`** (empty):

```python
"""Queen: the Telegram-owning front of the fleet."""
```

- [ ] **Step 4: Write `src/skep/queen/bookkeeping.py`**

```python
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    ref INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL,
    profile TEXT NOT NULL,
    local_id INTEGER NOT NULL,
    repo TEXT NOT NULL,
    title TEXT NOT NULL,
    topic_id INTEGER NOT NULL,
    activity_msg_id INTEGER,
    status TEXT NOT NULL DEFAULT 'running'
);
"""

_ACTIVE = ("spawning", "running")
_COLUMNS = ("ref", "host", "profile", "local_id", "repo", "title",
            "topic_id", "activity_msg_id", "status")


@dataclass
class Entry:
    ref: int
    host: str
    profile: str
    local_id: int
    repo: str
    title: str
    topic_id: int
    activity_msg_id: int | None
    status: str


class Bookkeeping:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    @classmethod
    def open(cls, path: str) -> "Bookkeeping":
        conn = sqlite3.connect(path)
        conn.executescript(_SCHEMA)
        conn.commit()
        return cls(conn)

    def _row(self, row: sqlite3.Row) -> Entry:
        return Entry(**{c: row[c] for c in _COLUMNS})

    def add(self, host: str, profile: str, local_id: int, repo: str,
            title: str, topic_id: int) -> int:
        cur = self._conn.execute(
            "INSERT INTO entries (host, profile, local_id, repo, title, topic_id,"
            " status) VALUES (?, ?, ?, ?, ?, ?, 'running')",
            (host, profile, local_id, repo, title, topic_id),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def by_worker_task(self, host: str, profile: str, local_id: int) -> Entry | None:
        row = self._conn.execute(
            "SELECT * FROM entries WHERE host=? AND profile=? AND local_id=?"
            " ORDER BY ref DESC LIMIT 1",
            (host, profile, local_id),
        ).fetchone()
        return self._row(row) if row else None

    def get(self, ref: int) -> Entry | None:
        row = self._conn.execute(
            "SELECT * FROM entries WHERE ref=?", (ref,)
        ).fetchone()
        return self._row(row) if row else None

    def set_activity_msg(self, ref: int, msg_id: int) -> None:
        self._conn.execute(
            "UPDATE entries SET activity_msg_id=? WHERE ref=?", (msg_id, ref)
        )
        self._conn.commit()

    def set_status(self, ref: int, status: str) -> None:
        self._conn.execute(
            "UPDATE entries SET status=? WHERE ref=?", (status, ref)
        )
        self._conn.commit()

    def list_active(self) -> list[Entry]:
        placeholders = ",".join("?" for _ in _ACTIVE)
        rows = self._conn.execute(
            f"SELECT * FROM entries WHERE status IN ({placeholders}) ORDER BY ref",
            _ACTIVE,
        ).fetchall()
        return [self._row(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_bookkeeping.py -v`
Expected: PASS (5 passed).

- [ ] **Step 6: Commit**

```bash
git add src/skep/queen/__init__.py src/skep/queen/bookkeeping.py tests/test_bookkeeping.py
git commit -m "feat: queen bookkeeping store (ref -> host/profile/task/topic mapping)"
```

---

## Task 7: Queen Telegram sink (QueenInbox → topics/messages)

**Files:**
- Create: `src/skep/queen/telegram_sink.py`
- Test: `tests/test_telegram_sink.py`

**Interfaces:**
- Consumes: `Gateway` (`skep.telegram_gw`), `Bookkeeping`, `escape_md` (`skep.formatting`).
- Produces: `QueenSink(gateway: Gateway, bookkeeping: Bookkeeping)` implementing `QueenInbox`:
  - `on_task_started` → `gateway.create_topic(f"{host}·{profile}·{repo}")`, `bookkeeping.add(...)`.
  - `on_activity` → first line `gateway.post(topic, escape_md(line))` + `set_activity_msg`; subsequent `gateway.edit(topic, msg_id, escape_md(line))`.
  - `on_milestone` → `gateway.post(topic, escape_md(text))`.
  - `on_done` → `bookkeeping.set_status(ref, status)`.

- [ ] **Step 1: Write the failing test** — `tests/test_telegram_sink.py`

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from skep.queen.bookkeeping import Bookkeeping
from skep.queen.telegram_sink import QueenSink


def _gateway():
    gw = MagicMock()
    gw.create_topic = AsyncMock(return_value=555)
    gw.post = AsyncMock(return_value=9)
    gw.edit = AsyncMock()
    return gw


async def test_task_started_creates_topic_and_entry():
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)
    await sink.on_task_started("g16", "work", 5, "nix", "clean nvidia")
    gw.create_topic.assert_awaited_once_with("g16·work·nix")
    e = bk.by_worker_task("g16", "work", 5)
    assert e.topic_id == 555


async def test_activity_posts_then_edits_and_escapes():
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)
    await sink.on_task_started("g16", "work", 5, "nix", "t")

    await sink.on_activity("g16", "work", 5, "🔧 edit_file")
    gw.post.assert_awaited_once_with(555, r"🔧 edit\_file")   # escaped on the queen
    ref = bk.by_worker_task("g16", "work", 5).ref
    assert bk.get(ref).activity_msg_id == 9

    await sink.on_activity("g16", "work", 5, "💬 v1.2")
    gw.edit.assert_awaited_once_with(555, 9, r"💬 v1\.2")


async def test_milestone_posts_escaped():
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)
    await sink.on_task_started("g16", "work", 5, "nix", "t")
    await sink.on_milestone("g16", "work", 5, "✅ Done: v1.2-3")
    gw.post.assert_awaited_with(555, r"✅ Done: v1\.2\-3")


async def test_done_sets_status():
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)
    await sink.on_task_started("g16", "work", 5, "nix", "t")
    await sink.on_done("g16", "work", 5, "done", "finished")
    ref = bk.by_worker_task("g16", "work", 5).ref
    assert bk.get(ref).status == "done"


async def test_activity_for_unknown_task_is_ignored():
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)
    # no on_task_started first — must not raise
    await sink.on_activity("g16", "work", 99, "orphan")
    gw.post.assert_not_awaited()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telegram_sink.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'skep.queen.telegram_sink'`.

- [ ] **Step 3: Write `src/skep/queen/telegram_sink.py`**

```python
from __future__ import annotations

from skep.formatting import escape_md
from skep.queen.bookkeeping import Bookkeeping
from skep.telegram_gw import Gateway


class QueenSink:
    """Implements QueenInbox: renders worker domain events into Telegram topics."""

    def __init__(self, gateway: Gateway, bookkeeping: Bookkeeping):
        self._gw = gateway
        self._bk = bookkeeping

    async def on_task_started(self, host: str, profile: str, local_id: int,
                              repo: str, title: str) -> None:
        topic_id = await self._gw.create_topic(f"{host}·{profile}·{repo}")
        self._bk.add(host, profile, local_id, repo, title, topic_id)

    async def on_activity(self, host: str, profile: str, local_id: int,
                          line: str) -> None:
        entry = self._bk.by_worker_task(host, profile, local_id)
        if entry is None:
            return
        text = escape_md(line)
        if entry.activity_msg_id is None:
            msg_id = await self._gw.post(entry.topic_id, text)
            self._bk.set_activity_msg(entry.ref, msg_id)
        else:
            await self._gw.edit(entry.topic_id, entry.activity_msg_id, text)

    async def on_milestone(self, host: str, profile: str, local_id: int,
                           text: str) -> None:
        entry = self._bk.by_worker_task(host, profile, local_id)
        if entry is None:
            return
        await self._gw.post(entry.topic_id, escape_md(text))

    async def on_done(self, host: str, profile: str, local_id: int,
                      status: str, summary: str) -> None:
        entry = self._bk.by_worker_task(host, profile, local_id)
        if entry is None:
            return
        self._bk.set_status(entry.ref, status)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_telegram_sink.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/skep/queen/telegram_sink.py tests/test_telegram_sink.py
git commit -m "feat: queen Telegram sink renders worker events into per-task topics"
```

---

## Task 8: Queen router — worker registry + commands

**Files:**
- Create: `src/skep/queen/router.py`
- Test: `tests/test_router.py`

**Interfaces:**
- Consumes: `Bookkeeping`, `CommandHandler` (`skep.transport`), `escape_md`.
- Produces:
  - `class UnknownWorker(Exception)`.
  - `class QueenRouter(bookkeeping: Bookkeeping)`:
    - `register(host: str, profile: str, handler: CommandHandler) -> None`.
    - `async cmd_spawn(host, profile, repo, task) -> None` — raises `UnknownWorker` if `(host, profile)` not registered; else `handler.spawn(repo, task)`.
    - `async cmd_kill(ref: int) -> bool` — looks up entry; routes `handler.kill(local_id)`; `False` if `ref` unknown.
    - `async cmd_panic() -> int` — `panic()` on every registered worker; returns worker count.
    - `format_ls() -> str` — MarkdownV2 table of active entries (`ref`, `host`, `profile`, `repo`, `status`).

- [ ] **Step 1: Write the failing test** — `tests/test_router.py`

```python
from unittest.mock import AsyncMock

import pytest

from skep.queen.bookkeeping import Bookkeeping
from skep.queen.router import QueenRouter, UnknownWorker


def _handler():
    h = AsyncMock()
    return h


async def test_spawn_routes_to_registered_worker():
    bk = Bookkeeping.open(":memory:")
    router = QueenRouter(bk)
    h = _handler()
    router.register("g16", "work", h)
    await router.cmd_spawn("g16", "work", "nix", "clean nvidia")
    h.spawn.assert_awaited_once_with("nix", "clean nvidia")


async def test_spawn_unknown_worker_raises():
    router = QueenRouter(Bookkeeping.open(":memory:"))
    with pytest.raises(UnknownWorker):
        await router.cmd_spawn("g16", "work", "nix", "t")


async def test_kill_routes_by_ref():
    bk = Bookkeeping.open(":memory:")
    router = QueenRouter(bk)
    h = _handler()
    router.register("g16", "work", h)
    ref = bk.add("g16", "work", 5, "nix", "t", topic_id=1)
    assert await router.cmd_kill(ref) is True
    h.kill.assert_awaited_once_with(5)


async def test_kill_unknown_ref_returns_false():
    router = QueenRouter(Bookkeeping.open(":memory:"))
    assert await router.cmd_kill(999) is False


async def test_panic_hits_all_workers():
    router = QueenRouter(Bookkeeping.open(":memory:"))
    h1, h2 = _handler(), _handler()
    router.register("g16", "work", h1)
    router.register("g16", "personal", h2)
    assert await router.cmd_panic() == 2
    h1.panic.assert_awaited_once()
    h2.panic.assert_awaited_once()


def test_format_ls_empty():
    assert "No active" in QueenRouter(Bookkeeping.open(":memory:")).format_ls()


def test_format_ls_lists_active_with_ref_host_profile():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("g16", "work", 5, "nix", "t", topic_id=1)
    out = QueenRouter(bk).format_ls()
    assert str(ref) in out
    assert "g16" in out and "work" in out and "nix" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_router.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'skep.queen.router'`.

- [ ] **Step 3: Write `src/skep/queen/router.py`**

```python
from __future__ import annotations

from skep.formatting import escape_md
from skep.queen.bookkeeping import Bookkeeping
from skep.transport import CommandHandler


class UnknownWorker(Exception):
    """Raised when a command targets a (host, profile) with no registered worker."""


class QueenRouter:
    def __init__(self, bookkeeping: Bookkeeping):
        self._bk = bookkeeping
        self._workers: dict[tuple[str, str], CommandHandler] = {}

    def register(self, host: str, profile: str, handler: CommandHandler) -> None:
        self._workers[(host, profile)] = handler

    async def cmd_spawn(self, host: str, profile: str, repo: str, task: str) -> None:
        handler = self._workers.get((host, profile))
        if handler is None:
            raise UnknownWorker(f"{host}/{profile}")
        await handler.spawn(repo, task)

    async def cmd_kill(self, ref: int) -> bool:
        entry = self._bk.get(ref)
        if entry is None:
            return False
        handler = self._workers.get((entry.host, entry.profile))
        if handler is None:
            return False
        await handler.kill(entry.local_id)
        return True

    async def cmd_panic(self) -> int:
        for handler in list(self._workers.values()):
            await handler.panic()
        return len(self._workers)

    def format_ls(self) -> str:
        entries = self._bk.list_active()
        if not entries:
            return "No active agents\\."
        lines = [
            f"`{e.ref}` {escape_md(e.host)}/{escape_md(e.profile)} "
            f"{escape_md(e.repo)} — {escape_md(e.status)}"
            for e in entries
        ]
        return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_router.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add src/skep/queen/router.py tests/test_router.py
git commit -m "feat: queen router — worker registry, spawn/kill/panic routing, /ls"
```

---

## Task 9: App wiring (interim single-process) + command handlers + integration

**Files:**
- Modify: `src/skep/app.py`
- Test: `tests/test_integration.py`

**Interfaces:**
- Consumes: everything above + `build_bot`, `Gateway`, `is_owner`.
- Produces:
  - `build_dispatcher(router: QueenRouter, config: QueenConfig) -> Dispatcher` — owner-gated handlers: `/spawn <host> [--profile <p>] <repo> <task>`, `/ls`, `/kill <ref>`, `/panic`.
  - `def parse_spawn(args: str) -> tuple[str, str, str, str] | None` — returns `(host, profile, repo, task)` or `None` if malformed. `--profile <p>` optional (default `"default"`).
  - `async def main()` / `def run()` — wire QueenConfig + WorkerConfig from env, one queen + one worker over the in-memory transport, start polling. **Interim**: Plan 2 splits this into `skep-queen`/`skepd`.

- [ ] **Step 1: Replace `tests/test_integration.py`**

```python
import asyncio

import pytest

from skep.app import build_worker_and_router, parse_spawn
from skep.config import QueenConfig, WorkerConfig
from skep.db import Registry
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.router import QueenRouter
from skep.queen.telegram_sink import QueenSink
from unittest.mock import AsyncMock, MagicMock


def test_parse_spawn_with_profile():
    assert parse_spawn("g16 --profile work nix clean the nvidia mess") == (
        "g16", "work", "nix", "clean the nvidia mess",
    )


def test_parse_spawn_default_profile():
    assert parse_spawn("g16 nix clean nvidia") == (
        "g16", "default", "nix", "clean nvidia",
    )


def test_parse_spawn_too_few_args_is_none():
    assert parse_spawn("g16 nix") is None
    assert parse_spawn("") is None


def _gateway():
    gw = MagicMock()
    gw.create_topic = AsyncMock(return_value=555)
    gw.post = AsyncMock(return_value=9)
    gw.edit = AsyncMock()
    return gw


async def test_end_to_end_spawn_with_fake_claude(tmp_path, git_repo, fake_claude_cmd):
    repo_name = git_repo.name
    wcfg = WorkerConfig(
        host="g16", profile="work", claude_config_dir=None,
        repos_root=git_repo.parent, worktrees_root=tmp_path / "wt",
        db_path=":memory:", max_concurrent=8, claude_bin=fake_claude_cmd,
    )
    gw = _gateway()
    bk = Bookkeeping.open(":memory:")
    router, supervisor = build_worker_and_router(wcfg, QueenSink(gw, bk), bk,
                                                 registry=Registry.open(":memory:"))

    await router.cmd_spawn("g16", "work", repo_name, "clean nvidia")

    for _ in range(200):
        entry = bk.by_worker_task("g16", "work", 1)
        if entry and entry.status in ("done", "failed", "killed"):
            break
        await asyncio.sleep(0.02)

    entry = bk.by_worker_task("g16", "work", 1)
    assert entry.status == "done"
    assert gw.create_topic.await_count == 1
    assert gw.post.await_count >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_integration.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_worker_and_router'`.

- [ ] **Step 3: Replace `src/skep/app.py`**

```python
from __future__ import annotations

import asyncio
import os

from aiogram import Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from skep.config import QueenConfig, WorkerConfig, load_queen_config, load_worker_config
from skep.db import Registry
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.router import QueenRouter, UnknownWorker
from skep.queen.telegram_sink import QueenSink
from skep.supervisor import CapacityError, Supervisor
from skep.telegram_gw import Gateway, build_bot, is_owner
from skep.transport import InMemoryEventSink


def parse_spawn(args: str) -> tuple[str, str, str, str] | None:
    """Parse `<host> [--profile <p>] <repo> <task...>` -> (host, profile, repo, task)."""
    tokens = (args or "").split()
    if len(tokens) < 3:
        return None
    host = tokens[0]
    rest = tokens[1:]
    profile = "default"
    if rest and rest[0] == "--profile":
        if len(rest) < 2:
            return None
        profile = rest[1]
        rest = rest[2:]
    if len(rest) < 2:
        return None
    repo = rest[0]
    task = " ".join(rest[1:])
    return host, profile, repo, task


def build_worker_and_router(
    wcfg: WorkerConfig, sink: QueenSink, bk: Bookkeeping, registry: Registry,
) -> tuple[QueenRouter, Supervisor]:
    """Wire one queen router + one worker over the in-memory transport (Plan 1)."""
    worker_sink = InMemoryEventSink(sink, wcfg.host, wcfg.profile)
    supervisor = Supervisor(wcfg, registry, worker_sink)
    router = QueenRouter(bk)
    router.register(wcfg.host, wcfg.profile, supervisor)
    return router, supervisor


def build_dispatcher(router: QueenRouter, config: QueenConfig) -> Dispatcher:
    dp = Dispatcher()

    async def owner_mw(handler, event, data):
        for attr in ("message", "edited_message", "callback_query"):
            sub = getattr(event, attr, None)
            user = getattr(sub, "from_user", None) if sub else None
            if user is not None:
                return await handler(event, data) if is_owner(user.id, config.owner_id) else None
        return None

    dp.update.outer_middleware(owner_mw)

    def owner_only(message: Message) -> bool:
        uid = message.from_user.id if message.from_user else None
        return is_owner(uid, config.owner_id)

    @dp.message(Command("spawn"), F.func(owner_only))
    async def _spawn(message: Message, command: CommandObject):
        parsed = parse_spawn(command.args or "")
        if parsed is None:
            await message.answer("Usage: /spawn <host> [--profile <p>] <repo> <task>",
                                 parse_mode=None)
            return
        host, profile, repo, task = parsed
        try:
            await router.cmd_spawn(host, profile, repo, task)
        except UnknownWorker:
            await message.answer(f"No worker for {host}/{profile}", parse_mode=None)
            return
        except CapacityError as exc:
            await message.answer(f"Rejected: {exc}", parse_mode=None)
            return
        await message.answer(f"Spawned on {host}/{profile}", parse_mode=None)

    @dp.message(Command("ls"), F.func(owner_only))
    async def _ls(message: Message):
        await message.answer(router.format_ls())

    @dp.message(Command("kill"), F.func(owner_only))
    async def _kill(message: Message, command: CommandObject):
        if not command.args or not command.args.strip().isdigit():
            await message.answer("Usage: /kill <ref>", parse_mode=None)
            return
        ok = await router.cmd_kill(int(command.args.strip()))
        await message.answer("Killed" if ok else "No such task", parse_mode=None)

    @dp.message(Command("panic"), F.func(owner_only))
    async def _panic(message: Message):
        n = await router.cmd_panic()
        await message.answer(f"Panicked {n} workers", parse_mode=None)

    return dp


async def main() -> None:
    qcfg = load_queen_config(os.environ)
    wcfg = load_worker_config(os.environ)
    bot = build_bot(qcfg)
    gateway = Gateway(bot, qcfg)
    bk = Bookkeeping.open(qcfg.bookkeeping_db)
    sink = QueenSink(gateway, bk)
    registry = Registry.open(wcfg.db_path)
    router, _ = build_worker_and_router(wcfg, sink, bk, registry)
    dp = build_dispatcher(router, qcfg)
    await dp.start_polling(bot)


def run() -> None:
    asyncio.run(main())
```

> Note: `build_bot(qcfg)` and `Gateway(bot, qcfg)` now take a `QueenConfig`. Both read only `.bot_token` / `.group_chat_id`, so no change is needed inside `telegram_gw.py` — but verify in Step 4.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -v`
Expected: PASS across all files. If `telegram_gw.py` referenced removed `Config` fields, it does not (it uses `config.bot_token` and `config.group_chat_id`, both present on `QueenConfig`) — no change needed.

- [ ] **Step 5: Commit**

```bash
git add src/skep/app.py tests/test_integration.py
git commit -m "feat: interim single-process wiring (queen+worker over in-memory transport)"
```

---

## Self-Review

**Spec coverage (Plan-1 slice of the Phase-2 spec):**
- Transport seam `EventSink`/`CommandHandler`/`QueenInbox` (spec §5) ✔ Task 2. In-memory transport ✔ Task 2 (WS is Plan 2).
- `Supervisor` refactored to emit domain events, drops topic bookkeeping (spec §11) ✔ Task 5.
- `host`/`profile` as separate fields through config, sink identity, bookkeeping, router (spec §4, §7, §8) ✔ Tasks 1, 2, 6, 8.
- `ref` global task handle; `/kill <ref>`; `/spawn <host> [--profile]` (spec §8) ✔ Tasks 6, 8, 9.
- Queen owns Telegram + formatting; escaping on the queen only (spec §3.1, §11) ✔ Tasks 3, 7.
- Queen bookkeeping SQLite (spec §3.1) ✔ Task 6.
- Profile isolation via `CLAUDE_CONFIG_DIR` (spec §4) ✔ Task 4 (+ wired in Task 5, asserted in Task 5's `test_spawn_...`).
- Capacity cap `max_concurrent` (spec §6.3) ✔ Tasks 1, 5.
- Owner-ID lock preserved (spec §9) ✔ Task 9 (outer middleware + `F.func(owner_only)`).

**Deferred to Plan 2 (not gaps):** WebSocket transport + two entrypoints, mutual challenge-response auth + per-hop encryption (§9), mDNS discovery (§6.1), heartbeat/presence + reconnection re-attach (§6.2, §6.4), queen restart topic reattachment (§13), containerized-queen/Caddy deploy (§10). The interim single-process `app.py` is explicitly replaced in Plan 2.

**Placeholder scan:** none — every code step is complete and runnable.

**Type consistency:** `EventSink`/`CommandHandler`/`QueenInbox` method names and signatures are identical across Tasks 2, 5, 7, 8. `WorkerConfig`/`QueenConfig` fields used in Tasks 1, 5, 9 match. `Bookkeeping` method names (`add`, `by_worker_task`, `get`, `set_activity_msg`, `set_status`, `list_active`) match across Tasks 6, 7, 8, 9. `Entry` fields consistent. `parse_spawn` return tuple order `(host, profile, repo, task)` matches its consumer in `_spawn`.

**Known interaction note (Task 5):** the capacity test depends on the first `spawn` inserting into `self._agents` synchronously before its `run_events` drains; the check counts `self._agents` at call time. If flakiness appears with the zero-event `FakeAgent`, make the fake yield one non-terminal event or have the test hold the slot via `sup._agents` injection (as `test_panic_kills_all_active` does).
