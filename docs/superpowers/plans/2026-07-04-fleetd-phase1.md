# fleetd Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A per-machine supervisor daemon (`fleetd`) that dispatches headless Claude Code agents from Telegram, streams their live activity into a per-task forum topic, and lets the owner list/kill them — with a hard owner-ID auth lock and an audit log.

**Architecture:** A single asyncio process. An aiogram long-polling bot receives `#control` commands from the owner only. On `/spawn`, a `Supervisor` creates a git worktree, spawns `claude -p … --output-format stream-json`, creates a Telegram forum topic, and streams parsed stream-json events into a single live-edited "activity" message plus durable milestone messages. All tasks are tracked in a SQLite registry that survives restart.

**Tech Stack:** Python 3.13, asyncio, aiogram 3.x (Telegram, long-polling), stdlib `sqlite3`, `uv` for packaging, `pytest` + `pytest-asyncio` for tests.

## Global Constraints

- Python **3.13**; asyncio throughout.
- Telegram library: **aiogram 3.x**, long-polling (`dp.start_polling`) — no webhook.
- Persistence: stdlib **`sqlite3`** (synchronous; registry ops are infrequent).
- **Auth:** every Telegram update is rejected unless `from_user.id == config.owner_id`. No exceptions.
- **Secrets:** bot token read from env (`FLEETD_BOT_TOKEN`); never written to disk or committed.
- **Agent runtime:** `claude -p "<task>" --output-format stream-json --input-format stream-json`, cwd = a fresh git worktree per task.
  > **Implementation note (final-review C1):** `--input-format stream-json` was **dropped for Phase 1**. Verified live against the real `claude`: with `--input-format stream-json` the process blocks reading its task from stdin until EOF, and fleetd (fire-and-forget in Phase 1) never writes/closes stdin → the agent hangs forever emitting no events. Phase 1 uses the one-shot form `claude -p "<task>" --output-format stream-json --verbose` with `stdin=DEVNULL`. `--input-format stream-json` + stdin writing returns in Phase 2 (soft-steer).
- **Mode:** Phase 1 is `native` only (no sandbox).
- **Cross-platform core:** modules under `src/fleetd/` make no OS-specific assumptions (no hardcoded paths, no systemd/Windows calls). Deployment wrappers are out of scope for this plan.
- Bot must hold the `can_manage_topics` admin right in the group (operational precondition, documented in README).

## File Structure

```
fleetd/
  pyproject.toml              # uv project: deps, pytest config
  README.md                   # setup + operational preconditions
  .gitignore
  src/fleetd/
    __init__.py
    config.py                 # Config dataclass + load_config(env)
    db.py                     # Registry (SQLite): Task model, CRUD, audit log
    stream.py                 # parse_event(line) -> Event | None
    formatting.py             # activity_line(event), milestone_message(event)
    agent.py                  # create_worktree(), AgentProcess (spawn/events/kill)
    telegram_gw.py            # build_bot(), OwnerFilter, topic + message helpers
    supervisor.py             # Supervisor: spawn orchestration, event loop, commands
    app.py                    # main() entrypoint
  tests/
    conftest.py               # shared fixtures (temp git repo, fake claude stub)
    fixtures/fake_claude.py   # canned stream-json emitter (test double for `claude`)
    test_config.py
    test_db.py
    test_stream.py
    test_formatting.py
    test_agent.py
    test_telegram_gw.py
    test_supervisor.py
    test_integration.py
```

---

## Task 1: Project scaffold + Config

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `README.md`, `src/fleetd/__init__.py`, `src/fleetd/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config` (frozen dataclass) with fields `bot_token: str`, `owner_id: int`, `group_chat_id: int`, `repos_root: Path`, `worktrees_root: Path`, `claude_bin: str`; and `load_config(env: Mapping[str, str]) -> Config`.

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "fleetd"
version = "0.1.0"
description = "Telegram control plane for headless Claude Code agents"
requires-python = ">=3.13"
dependencies = ["aiogram>=3.13"]

[project.scripts]
fleetd = "fleetd.app:run"

[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio>=0.24"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
pythonpath = ["src"]
testpaths = ["tests"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/fleetd"]
```

- [ ] **Step 2: Create `.gitignore`**

```gitignore
__pycache__/
*.pyc
.venv/
.pytest_cache/
*.sqlite
*.sqlite-journal
.env
```

- [ ] **Step 3: Create `README.md`**

```markdown
# fleetd

Per-machine supervisor daemon that dispatches headless Claude Code agents from
Telegram. See `docs/superpowers/specs/` for the design.

## Setup

1. Create a Telegram bot via @BotFather; note the token.
2. Create a supergroup, enable **Topics**, add the bot as admin with the
   **Manage Topics** right.
3. Get your own Telegram user ID (e.g. via @userinfobot) and the group chat ID.
4. Export config (never commit these):

   ```sh
   export FLEETD_BOT_TOKEN=...        # from BotFather
   export FLEETD_OWNER_ID=123456789   # your user ID; only you may command the bot
   export FLEETD_GROUP_CHAT_ID=-100...# the supergroup chat ID
   export FLEETD_REPOS_ROOT=$HOME/gh  # where your repos live
   export FLEETD_WORKTREES_ROOT=$HOME/.fleetd/worktrees
   # optional: FLEETD_CLAUDE_BIN=claude
   ```

5. `uv run fleetd`

## Develop

`uv run pytest`
```

- [ ] **Step 4: Create `src/fleetd/__init__.py`**

```python
"""fleetd — Telegram control plane for headless Claude Code agents."""
```

- [ ] **Step 5: Write the failing test** — `tests/test_config.py`

```python
from pathlib import Path

import pytest

from fleetd.config import Config, load_config


def _base_env():
    return {
        "FLEETD_BOT_TOKEN": "tok",
        "FLEETD_OWNER_ID": "42",
        "FLEETD_GROUP_CHAT_ID": "-1001",
        "FLEETD_REPOS_ROOT": "/home/me/gh",
        "FLEETD_WORKTREES_ROOT": "/home/me/.fleetd/worktrees",
    }


def test_load_config_parses_all_fields():
    cfg = load_config(_base_env())
    assert cfg == Config(
        bot_token="tok",
        owner_id=42,
        group_chat_id=-1001,
        repos_root=Path("/home/me/gh"),
        worktrees_root=Path("/home/me/.fleetd/worktrees"),
        claude_bin="claude",
    )


def test_load_config_claude_bin_override():
    env = _base_env() | {"FLEETD_CLAUDE_BIN": "/opt/claude"}
    assert load_config(env).claude_bin == "/opt/claude"


def test_load_config_missing_required_raises():
    env = _base_env()
    del env["FLEETD_BOT_TOKEN"]
    with pytest.raises(KeyError):
        load_config(env)


def test_config_is_frozen():
    cfg = load_config(_base_env())
    with pytest.raises(Exception):
        cfg.owner_id = 1  # type: ignore[misc]
```

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleetd.config'`

- [ ] **Step 7: Write `src/fleetd/config.py`**

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    group_chat_id: int
    repos_root: Path
    worktrees_root: Path
    claude_bin: str = "claude"


def load_config(env: Mapping[str, str]) -> Config:
    return Config(
        bot_token=env["FLEETD_BOT_TOKEN"],
        owner_id=int(env["FLEETD_OWNER_ID"]),
        group_chat_id=int(env["FLEETD_GROUP_CHAT_ID"]),
        repos_root=Path(env["FLEETD_REPOS_ROOT"]),
        worktrees_root=Path(env["FLEETD_WORKTREES_ROOT"]),
        claude_bin=env.get("FLEETD_CLAUDE_BIN", "claude"),
    )
```

- [ ] **Step 8: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (4 passed)

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml .gitignore README.md src/fleetd/__init__.py src/fleetd/config.py tests/test_config.py
git commit -m "feat: project scaffold + config loading"
```

---

## Task 2: SQLite registry

**Files:**
- Create: `src/fleetd/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Task` dataclass: `id: int | None`, `repo: str`, `task: str`, `worktree_path: str`, `session_id: str | None`, `pid: int | None`, `topic_id: int | None`, `mode: str`, `status: str`, `created_at: str`.
  - `Registry` with: `Registry.open(path: str) -> Registry` (creates schema), `add_task(repo, task, worktree_path, mode="native") -> int`, `get_task(task_id: int) -> Task | None`, `list_active() -> list[Task]`, `list_all() -> list[Task]`, `update(task_id: int, **fields) -> None`, `log_audit(task_id: int | None, kind: str, detail: str) -> None`, `close() -> None`.
  - Status values used across the codebase: `"spawning"`, `"running"`, `"done"`, `"killed"`, `"failed"`. Active = `spawning` or `running`.

- [ ] **Step 1: Write the failing test** — `tests/test_db.py`

```python
from fleetd.db import Registry, Task


def test_add_and_get_task():
    reg = Registry.open(":memory:")
    tid = reg.add_task("nix", "clean nvidia", "/wt/nix-1")
    task = reg.get_task(tid)
    assert task.id == tid
    assert task.repo == "nix"
    assert task.task == "clean nvidia"
    assert task.worktree_path == "/wt/nix-1"
    assert task.mode == "native"
    assert task.status == "spawning"
    assert task.created_at  # non-empty ISO timestamp


def test_update_fields():
    reg = Registry.open(":memory:")
    tid = reg.add_task("nix", "t", "/wt/1")
    reg.update(tid, status="running", pid=999, session_id="sess-1", topic_id=7)
    task = reg.get_task(tid)
    assert (task.status, task.pid, task.session_id, task.topic_id) == (
        "running", 999, "sess-1", 7,
    )


def test_list_active_excludes_terminal():
    reg = Registry.open(":memory:")
    a = reg.add_task("r", "a", "/wt/a")
    b = reg.add_task("r", "b", "/wt/b")
    reg.update(a, status="running")
    reg.update(b, status="done")
    active = reg.list_active()
    assert [t.id for t in active] == [a]


def test_get_missing_returns_none():
    reg = Registry.open(":memory:")
    assert reg.get_task(123) is None


def test_audit_log_persists_rows():
    reg = Registry.open(":memory:")
    tid = reg.add_task("r", "t", "/wt/1")
    reg.log_audit(tid, "spawn", "claude -p ...")
    reg.log_audit(None, "panic", "fleet-wide kill")
    rows = reg.audit_rows()
    assert [(r["kind"], r["detail"]) for r in rows] == [
        ("spawn", "claude -p ..."),
        ("panic", "fleet-wide kill"),
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleetd.db'`

- [ ] **Step 3: Write `src/fleetd/db.py`**

```python
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    task TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    session_id TEXT,
    pid INTEGER,
    topic_id INTEGER,
    mode TEXT NOT NULL DEFAULT 'native',
    status TEXT NOT NULL DEFAULT 'spawning',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    at TEXT NOT NULL
);
"""

_ACTIVE = ("spawning", "running")
_TASK_COLUMNS = (
    "id", "repo", "task", "worktree_path", "session_id",
    "pid", "topic_id", "mode", "status", "created_at",
)


@dataclass
class Task:
    id: int | None
    repo: str
    task: str
    worktree_path: str
    session_id: str | None
    pid: int | None
    topic_id: int | None
    mode: str
    status: str
    created_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Registry:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    @classmethod
    def open(cls, path: str) -> "Registry":
        conn = sqlite3.connect(path)
        conn.executescript(_SCHEMA)
        conn.commit()
        return cls(conn)

    def add_task(self, repo: str, task: str, worktree_path: str,
                 mode: str = "native") -> int:
        cur = self._conn.execute(
            "INSERT INTO tasks (repo, task, worktree_path, mode, status, created_at)"
            " VALUES (?, ?, ?, ?, 'spawning', ?)",
            (repo, task, worktree_path, mode, _now()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        return Task(**{c: row[c] for c in _TASK_COLUMNS})

    def get_task(self, task_id: int) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return self._row_to_task(row) if row else None

    def list_all(self) -> list[Task]:
        rows = self._conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        return [self._row_to_task(r) for r in rows]

    def list_active(self) -> list[Task]:
        placeholders = ",".join("?" for _ in _ACTIVE)
        rows = self._conn.execute(
            f"SELECT * FROM tasks WHERE status IN ({placeholders}) ORDER BY id",
            _ACTIVE,
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def update(self, task_id: int, **fields) -> None:
        if not fields:
            return
        allowed = set(_TASK_COLUMNS) - {"id", "created_at"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update columns: {bad}")
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?",
            (*fields.values(), task_id),
        )
        self._conn.commit()

    def log_audit(self, task_id: int | None, kind: str, detail: str) -> None:
        self._conn.execute(
            "INSERT INTO audit (task_id, kind, detail, at) VALUES (?, ?, ?, ?)",
            (task_id, kind, detail, _now()),
        )
        self._conn.commit()

    def audit_rows(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM audit ORDER BY id").fetchall()

    def close(self) -> None:
        self._conn.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_db.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/fleetd/db.py tests/test_db.py
git commit -m "feat: SQLite task registry + audit log"
```

---

## Task 3: stream-json event parser

**Files:**
- Create: `src/fleetd/stream.py`
- Test: `tests/test_stream.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Event` dataclass: `kind: str`, `text: str = ""`, `tool_name: str = ""`, `session_id: str | None = None`, `is_error: bool = False`, `raw: dict = {}`.
  - `kind` is one of `"system"`, `"assistant_text"`, `"tool_use"`, `"tool_result"`, `"result"`, `"unknown"`.
  - `parse_event(line: str) -> Event | None` — returns `None` for blank/whitespace lines and lines that are not valid JSON objects.

- [ ] **Step 1: Write the failing test** — `tests/test_stream.py`

```python
import json

from fleetd.stream import Event, parse_event


def test_blank_line_returns_none():
    assert parse_event("   ") is None
    assert parse_event("") is None


def test_non_json_returns_none():
    assert parse_event("not json at all") is None


def test_system_init_carries_session_id():
    line = json.dumps({"type": "system", "subtype": "init", "session_id": "s1"})
    ev = parse_event(line)
    assert ev.kind == "system"
    assert ev.session_id == "s1"


def test_assistant_text_block():
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "Hello there"}]},
    })
    ev = parse_event(line)
    assert ev.kind == "assistant_text"
    assert ev.text == "Hello there"


def test_assistant_tool_use_block():
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "edit_file", "input": {"path": "a.py"}},
        ]},
    })
    ev = parse_event(line)
    assert ev.kind == "tool_use"
    assert ev.tool_name == "edit_file"


def test_user_tool_result_block():
    line = json.dumps({
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": "ok"}]},
    })
    ev = parse_event(line)
    assert ev.kind == "tool_result"


def test_result_event_captures_error_and_session():
    line = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "done", "session_id": "s1",
    })
    ev = parse_event(line)
    assert ev.kind == "result"
    assert ev.is_error is False
    assert ev.text == "done"
    assert ev.session_id == "s1"


def test_unknown_type_is_unknown_kind():
    ev = parse_event(json.dumps({"type": "weird"}))
    assert ev.kind == "unknown"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_stream.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleetd.stream'`

- [ ] **Step 3: Write `src/fleetd/stream.py`**

```python
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class Event:
    kind: str
    text: str = ""
    tool_name: str = ""
    session_id: str | None = None
    is_error: bool = False
    raw: dict = field(default_factory=dict)


def _first_blocks(obj: dict) -> list[dict]:
    content = obj.get("message", {}).get("content", [])
    return content if isinstance(content, list) else []


def parse_event(line: str) -> Event | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None

    etype = obj.get("type")
    if etype == "system":
        return Event(kind="system", session_id=obj.get("session_id"), raw=obj)
    if etype == "result":
        return Event(
            kind="result",
            text=obj.get("result", "") or "",
            session_id=obj.get("session_id"),
            is_error=bool(obj.get("is_error", False)),
            raw=obj,
        )
    if etype == "assistant":
        for block in _first_blocks(obj):
            if block.get("type") == "text":
                return Event(kind="assistant_text", text=block.get("text", ""), raw=obj)
            if block.get("type") == "tool_use":
                return Event(kind="tool_use", tool_name=block.get("name", ""), raw=obj)
        return Event(kind="unknown", raw=obj)
    if etype == "user":
        for block in _first_blocks(obj):
            if block.get("type") == "tool_result":
                return Event(kind="tool_result", raw=obj)
        return Event(kind="unknown", raw=obj)
    return Event(kind="unknown", raw=obj)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_stream.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add src/fleetd/stream.py tests/test_stream.py
git commit -m "feat: stream-json event parser"
```

---

## Task 4: Telegram formatting

**Files:**
- Create: `src/fleetd/formatting.py`
- Test: `tests/test_formatting.py`

**Interfaces:**
- Consumes: `Event` from `fleetd.stream`.
- Produces:
  - `activity_line(event: Event) -> str | None` — the short line for the live-edited activity message, or `None` if the event should not update it.
  - `milestone_message(event: Event) -> str | None` — a durable message, or `None` if the event is not a milestone.
  - `escape_md(text: str) -> str` — escape text for Telegram MarkdownV2.

- [ ] **Step 1: Write the failing test** — `tests/test_formatting.py`

```python
from fleetd.formatting import activity_line, escape_md, milestone_message
from fleetd.stream import Event


def test_escape_md_escapes_reserved_chars():
    assert escape_md("a_b*c[d]") == r"a\_b\*c\[d\]"
    assert escape_md("v1.2-3") == r"v1\.2\-3"


def test_activity_line_for_assistant_text():
    ev = Event(kind="assistant_text", text="Refactoring the module")
    assert activity_line(ev) == "💬 Refactoring the module"


def test_activity_line_for_tool_use():
    ev = Event(kind="tool_use", tool_name="edit_file")
    assert activity_line(ev) == "🔧 edit\\_file"


def test_activity_line_none_for_tool_result():
    assert activity_line(Event(kind="tool_result")) is None


def test_milestone_for_successful_result():
    ev = Event(kind="result", text="All done", is_error=False)
    assert milestone_message(ev) == "✅ Done: All done"


def test_milestone_for_error_result():
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
Expected: FAIL — `ModuleNotFoundError: No module named 'fleetd.formatting'`

- [ ] **Step 3: Write `src/fleetd/formatting.py`**

```python
from __future__ import annotations

from fleetd.stream import Event

_MD_RESERVED = r"_*[]()~`>#+-=|{}.!"
_MAX_ACTIVITY = 200


def escape_md(text: str) -> str:
    out = []
    for ch in text:
        if ch in _MD_RESERVED:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _truncate(text: str) -> str:
    if len(text) <= _MAX_ACTIVITY:
        return text
    return text[: _MAX_ACTIVITY - 1] + "…"


def activity_line(event: Event) -> str | None:
    if event.kind == "assistant_text":
        return _truncate("💬 " + escape_md(event.text))
    if event.kind == "tool_use":
        return _truncate("🔧 " + escape_md(event.tool_name))
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
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add src/fleetd/formatting.py tests/test_formatting.py
git commit -m "feat: Telegram MarkdownV2 formatting for agent events"
```

---

## Task 5: Agent process manager

**Files:**
- Create: `src/fleetd/agent.py`, `tests/conftest.py`, `tests/fixtures/fake_claude.py`
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: `Event`, `parse_event` from `fleetd.stream`.
- Produces:
  - `create_worktree(repo_path: Path, worktree_path: Path, branch: str) -> None` — runs `git -C <repo_path> worktree add -b <branch> <worktree_path>`.
  - `AgentProcess(task_text: str, cwd: Path, claude_bin: str)` with:
    - `async start() -> None` — spawns the subprocess in `cwd`.
    - `async events() -> AsyncIterator[Event]` — yields parsed non-`None` events from stdout until the process exits.
    - `async kill() -> None` — terminates the process.
    - property `pid: int | None`.

- [ ] **Step 1: Create the fake claude stub** — `tests/fixtures/fake_claude.py`

```python
"""Test double for the `claude` binary: emits canned stream-json, then exits.

Usage mirrors the real invocation enough for AgentProcess: the prompt is passed
as the last positional arg; flags are ignored. Emits a system init, one
assistant text, one tool_use, and a result line.
"""
import json
import sys
import time


def main() -> None:
    prompt = sys.argv[-1]
    lines = [
        {"type": "system", "subtype": "init", "session_id": "fake-sess"},
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": f"working on: {prompt}"}]}},
        {"type": "assistant",
         "message": {"content": [{"type": "tool_use", "name": "edit_file", "input": {}}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": "finished", "session_id": "fake-sess"},
    ]
    for obj in lines:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()
        time.sleep(0.01)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Create shared fixtures** — `tests/conftest.py`

```python
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
```

- [ ] **Step 3: Write the failing test** — `tests/test_agent.py`

```python
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
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run pytest tests/test_agent.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleetd.agent'`

- [ ] **Step 5: Write `src/fleetd/agent.py`**

```python
from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import AsyncIterator

from fleetd.stream import Event, parse_event


def create_worktree(repo_path: Path, worktree_path: Path, branch: str) -> None:
    import subprocess

    subprocess.run(
        ["git", "-C", str(repo_path), "worktree", "add", "-b", branch,
         str(worktree_path)],
        check=True,
        capture_output=True,
    )


class AgentProcess:
    def __init__(self, task_text: str, cwd: Path, claude_bin: str):
        self._task_text = task_text
        self._cwd = cwd
        self._claude_bin = claude_bin
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def _argv(self) -> list[str]:
        # claude_bin may be a multi-token command (e.g. "python fake_claude.py").
        base = shlex.split(self._claude_bin)
        return [
            *base, "-p", self._task_text,
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
        ]

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv(),
            cwd=str(self._cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def events(self) -> AsyncIterator[Event]:
        assert self._proc is not None and self._proc.stdout is not None
        async for raw in self._proc.stdout:
            ev = parse_event(raw.decode(errors="replace"))
            if ev is not None:
                yield ev
        await self._proc.wait()

    async def kill(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_agent.py -v`
Expected: PASS (3 passed)

- [ ] **Step 7: Commit**

```bash
git add src/fleetd/agent.py tests/test_agent.py tests/conftest.py tests/fixtures/fake_claude.py
git commit -m "feat: agent process manager + git worktree creation"
```

---

## Task 6: Telegram gateway (bot, auth, topic + message helpers)

**Files:**
- Create: `src/fleetd/telegram_gw.py`
- Test: `tests/test_telegram_gw.py`

**Interfaces:**
- Consumes: `Config` from `fleetd.config`.
- Produces:
  - `is_owner(user_id: int | None, owner_id: int) -> bool`.
  - `class Gateway` wrapping an aiogram `Bot`, constructed as `Gateway(bot, config)`, with:
    - `async create_topic(name: str) -> int` — returns the new topic's `message_thread_id`.
    - `async delete_topic(topic_id: int) -> None`.
    - `async post(topic_id: int, text: str) -> int` — sends a MarkdownV2 message, returns its `message_id`.
    - `async edit(topic_id: int, message_id: int, text: str) -> None` — edits a message; swallows aiogram "message is not modified" errors.
  - `build_bot(config: Config)` returning an aiogram `Bot` with MarkdownV2 default parse mode.

- [ ] **Step 1: Write the failing test** — `tests/test_telegram_gw.py`

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from fleetd.config import Config
from fleetd.telegram_gw import Gateway, is_owner


def _cfg():
    from pathlib import Path
    return Config("tok", 42, -1001, Path("/r"), Path("/w"))


def test_is_owner():
    assert is_owner(42, 42) is True
    assert is_owner(7, 42) is False
    assert is_owner(None, 42) is False


async def test_create_topic_returns_thread_id():
    bot = MagicMock()
    bot.create_forum_topic = AsyncMock(
        return_value=MagicMock(message_thread_id=555)
    )
    gw = Gateway(bot, _cfg())
    tid = await gw.create_topic("nix · task")
    assert tid == 555
    bot.create_forum_topic.assert_awaited_once_with(
        chat_id=-1001, name="nix · task"
    )


async def test_post_returns_message_id():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=9))
    gw = Gateway(bot, _cfg())
    mid = await gw.post(555, "hello")
    assert mid == 9
    bot.send_message.assert_awaited_once_with(
        chat_id=-1001, message_thread_id=555, text="hello"
    )


async def test_edit_swallows_not_modified():
    from aiogram.exceptions import TelegramBadRequest

    bot = MagicMock()
    bot.edit_message_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(),
                                       message="message is not modified")
    )
    gw = Gateway(bot, _cfg())
    # must not raise
    await gw.edit(555, 9, "same text")


async def test_delete_topic_calls_bot():
    bot = MagicMock()
    bot.delete_forum_topic = AsyncMock()
    gw = Gateway(bot, _cfg())
    await gw.delete_topic(555)
    bot.delete_forum_topic.assert_awaited_once_with(
        chat_id=-1001, message_thread_id=555
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telegram_gw.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleetd.telegram_gw'`

- [ ] **Step 3: Write `src/fleetd/telegram_gw.py`**

```python
from __future__ import annotations

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest

from fleetd.config import Config


def is_owner(user_id: int | None, owner_id: int) -> bool:
    return user_id is not None and user_id == owner_id


def build_bot(config: Config) -> Bot:
    return Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2),
    )


class Gateway:
    def __init__(self, bot: Bot, config: Config):
        self._bot = bot
        self._chat_id = config.group_chat_id

    async def create_topic(self, name: str) -> int:
        topic = await self._bot.create_forum_topic(chat_id=self._chat_id, name=name)
        return topic.message_thread_id

    async def delete_topic(self, topic_id: int) -> None:
        await self._bot.delete_forum_topic(
            chat_id=self._chat_id, message_thread_id=topic_id
        )

    async def post(self, topic_id: int, text: str) -> int:
        msg = await self._bot.send_message(
            chat_id=self._chat_id, message_thread_id=topic_id, text=text
        )
        return msg.message_id

    async def edit(self, topic_id: int, message_id: int, text: str) -> None:
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id, message_id=message_id, text=text
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_telegram_gw.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/fleetd/telegram_gw.py tests/test_telegram_gw.py
git commit -m "feat: Telegram gateway — auth, topic + message helpers"
```

---

## Task 7: Supervisor — spawn orchestration + commands

**Files:**
- Create: `src/fleetd/supervisor.py`
- Test: `tests/test_supervisor.py`

**Interfaces:**
- Consumes: `Config`, `Registry`, `Gateway`, `AgentProcess`, `create_worktree`, `activity_line`, `milestone_message`.
- Produces:
  - `class Supervisor(config, registry, gateway, agent_factory=AgentProcess, worktree_factory=create_worktree)`.
    - `async spawn(repo: str, task_text: str) -> int` — creates worktree + topic, records task, starts the agent, launches the event loop as a background task, returns the task id.
    - `async run_events(task_id: int, agent: AgentProcess) -> None` — consumes agent events, live-edits the activity message, posts milestones, updates registry status/session_id, marks terminal state.
    - `async kill(task_id: int) -> bool` — kills a running agent; returns False if unknown/not active.
    - `async panic() -> int` — kills all active agents; returns count.
    - `def list_active() -> list[Task]`.
  - `agent_factory(task_text, cwd, claude_bin) -> AgentProcess` and `worktree_factory(repo_path, worktree_path, branch)` are injected for testing.

- [ ] **Step 1: Write the failing test** — `tests/test_supervisor.py`

```python
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from fleetd.config import Config
from fleetd.db import Registry
from fleetd.stream import Event
from fleetd.supervisor import Supervisor


def _cfg(tmp_path):
    return Config("tok", 42, -1001, tmp_path / "repos", tmp_path / "wt")


class FakeAgent:
    def __init__(self, events):
        self._events = events
        self.pid = 123
        self.killed = False
        self.started = False

    async def start(self):
        self.started = True

    async def events(self):
        for ev in self._events:
            yield ev

    async def kill(self):
        self.killed = True


def _gateway():
    gw = MagicMock()
    gw.create_topic = AsyncMock(return_value=555)
    gw.post = AsyncMock(return_value=9)
    gw.edit = AsyncMock()
    gw.delete_topic = AsyncMock()
    return gw


async def test_spawn_creates_worktree_topic_and_task(tmp_path):
    cfg = _cfg(tmp_path)
    (cfg.repos_root / "nix").mkdir(parents=True)
    reg = Registry.open(":memory:")
    gw = _gateway()
    created = {}

    def wt_factory(repo_path, worktree_path, branch):
        created["repo_path"] = repo_path
        created["branch"] = branch

    agent = FakeAgent([Event(kind="system", session_id="s9")])
    sup = Supervisor(cfg, reg, gw,
                     agent_factory=lambda **k: agent,
                     worktree_factory=wt_factory)
    tid = await sup.spawn("nix", "clean nvidia")

    task = reg.get_task(tid)
    assert task.repo == "nix"
    assert task.topic_id == 555
    assert created["repo_path"] == cfg.repos_root / "nix"
    gw.create_topic.assert_awaited_once()


async def test_run_events_edits_activity_and_marks_done(tmp_path):
    cfg = _cfg(tmp_path)
    reg = Registry.open(":memory:")
    gw = _gateway()
    events = [
        Event(kind="system", session_id="s9"),
        Event(kind="assistant_text", text="hi"),
        Event(kind="tool_use", tool_name="edit_file"),
        Event(kind="result", text="finished", is_error=False),
    ]
    agent = FakeAgent(events)
    sup = Supervisor(cfg, reg, gw,
                     agent_factory=lambda **k: agent,
                     worktree_factory=lambda *a, **k: None)
    tid = reg.add_task("nix", "t", str(tmp_path / "wt"))
    reg.update(tid, topic_id=555)

    await sup.run_events(tid, agent)

    task = reg.get_task(tid)
    assert task.status == "done"
    assert task.session_id == "s9"
    assert gw.edit.await_count >= 1          # activity updated
    assert gw.post.await_count >= 1          # milestone posted


async def test_kill_unknown_returns_false(tmp_path):
    sup = Supervisor(_cfg(tmp_path), Registry.open(":memory:"), _gateway())
    assert await sup.kill(999) is False


async def test_panic_kills_all_active(tmp_path):
    cfg = _cfg(tmp_path)
    reg = Registry.open(":memory:")
    sup = Supervisor(cfg, reg, _gateway())
    a1, a2 = FakeAgent([]), FakeAgent([])
    t1 = reg.add_task("r", "a", "/wt/a"); reg.update(t1, status="running")
    t2 = reg.add_task("r", "b", "/wt/b"); reg.update(t2, status="running")
    sup._agents = {t1: a1, t2: a2}          # inject live agents
    n = await sup.panic()
    assert n == 2
    assert a1.killed and a2.killed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_supervisor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleetd.supervisor'`

- [ ] **Step 3: Write `src/fleetd/supervisor.py`**

```python
from __future__ import annotations

import asyncio
import re

from fleetd.agent import AgentProcess, create_worktree
from fleetd.config import Config
from fleetd.db import Registry, Task
from fleetd.formatting import activity_line, milestone_message
from fleetd.telegram_gw import Gateway


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:24] or "task"


class Supervisor:
    def __init__(self, config: Config, registry: Registry, gateway: Gateway,
                 agent_factory=AgentProcess, worktree_factory=create_worktree):
        self._cfg = config
        self._reg = registry
        self._gw = gateway
        self._agent_factory = agent_factory
        self._worktree_factory = worktree_factory
        self._agents: dict[int, AgentProcess] = {}

    def list_active(self) -> list[Task]:
        return self._reg.list_active()

    async def spawn(self, repo: str, task_text: str) -> int:
        repo_path = self._cfg.repos_root / repo
        # reserve an id so the worktree/branch names are unique
        tid = self._reg.add_task(repo, task_text, "", mode="native")
        branch = f"fleetd/{_slug(task_text)}-{tid}"
        worktree_path = self._cfg.worktrees_root / f"{repo}-{tid}"
        self._reg.update(tid, worktree_path=str(worktree_path))

        self._worktree_factory(repo_path, worktree_path, branch)
        topic_id = await self._gw.create_topic(f"{repo} · {_slug(task_text)}")
        self._reg.update(tid, topic_id=topic_id)
        self._reg.log_audit(tid, "spawn", f"{repo}: {task_text}")

        agent = self._agent_factory(
            task_text=task_text, cwd=worktree_path, claude_bin=self._cfg.claude_bin
        )
        await agent.start()
        self._agents[tid] = agent
        self._reg.update(tid, status="running", pid=agent.pid)

        asyncio.create_task(self.run_events(tid, agent))
        return tid

    async def run_events(self, task_id: int, agent: AgentProcess) -> None:
        topic_id = self._reg.get_task(task_id).topic_id
        activity_msg_id: int | None = None
        terminal = "done"
        try:
            async for ev in agent.events():
                if ev.kind == "system" and ev.session_id:
                    self._reg.update(task_id, session_id=ev.session_id)
                if ev.kind == "result":
                    self._reg.update(task_id, session_id=ev.session_id or
                                     self._reg.get_task(task_id).session_id)
                    terminal = "failed" if ev.is_error else "done"

                line = activity_line(ev)
                if line is not None:
                    if activity_msg_id is None:
                        activity_msg_id = await self._gw.post(topic_id, line)
                    else:
                        await self._gw.edit(topic_id, activity_msg_id, line)

                milestone = milestone_message(ev)
                if milestone is not None:
                    await self._gw.post(topic_id, milestone)
        finally:
            if self._reg.get_task(task_id).status == "killed":
                terminal = "killed"
            self._reg.update(task_id, status=terminal)
            self._agents.pop(task_id, None)

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
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/fleetd/supervisor.py tests/test_supervisor.py
git commit -m "feat: supervisor — spawn orchestration, event loop, kill/panic"
```

---

## Task 8: App wiring + command handlers + integration test

**Files:**
- Create: `src/fleetd/app.py`
- Test: `tests/test_integration.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `build_dispatcher(supervisor: Supervisor, config: Config) -> Dispatcher` — registers owner-gated handlers for `/spawn <repo> <task...>`, `/ls`, `/kill <id>`, `/panic`.
  - `def format_ls(tasks: list[Task]) -> str` — renders the active-task list for `/ls`.
  - `async def main() -> None` — loads config from env, opens the registry, builds the bot/gateway/supervisor/dispatcher, and starts long-polling.
  - `def run() -> None` — `asyncio.run(main())` (the `fleetd` console-script entry point).

- [ ] **Step 1: Write the failing test** — `tests/test_integration.py`

```python
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from fleetd.app import format_ls
from fleetd.config import Config
from fleetd.db import Registry
from fleetd.supervisor import Supervisor


def _cfg(tmp_path):
    return Config("tok", 42, -1001, tmp_path / "repos", tmp_path / "wt",
                  claude_bin="")  # set per-test


def _gateway():
    gw = MagicMock()
    gw.create_topic = AsyncMock(return_value=555)
    gw.post = AsyncMock(return_value=9)
    gw.edit = AsyncMock()
    gw.delete_topic = AsyncMock()
    return gw


def test_format_ls_empty():
    assert "No active" in format_ls([])


def test_format_ls_lists_tasks():
    reg = Registry.open(":memory:")
    tid = reg.add_task("nix", "clean nvidia", "/wt/nix-1")
    reg.update(tid, status="running")
    out = format_ls(reg.list_active())
    assert "nix" in out
    assert str(tid) in out


async def test_end_to_end_spawn_with_fake_claude(tmp_path, git_repo,
                                                  fake_claude_cmd):
    import asyncio

    cfg = Config("tok", 42, -1001, git_repo.parent, tmp_path / "wt",
                 claude_bin=fake_claude_cmd)
    # repo dir must be named for the spawn arg
    repo_name = git_repo.name
    reg = Registry.open(":memory:")
    gw = _gateway()
    sup = Supervisor(cfg, reg, gw)

    tid = await sup.spawn(repo_name, "clean nvidia")
    # wait for the background event loop to finish
    for _ in range(200):
        if reg.get_task(tid).status in ("done", "failed", "killed"):
            break
        await asyncio.sleep(0.02)

    task = reg.get_task(tid)
    assert task.status == "done"
    assert task.session_id == "fake-sess"
    assert gw.create_topic.await_count == 1
    assert gw.post.await_count >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_integration.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleetd.app'`

- [ ] **Step 3: Write `src/fleetd/app.py`**

```python
from __future__ import annotations

import asyncio
import os

from aiogram import Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from fleetd.config import Config, load_config
from fleetd.db import Registry, Task
from fleetd.supervisor import Supervisor
from fleetd.telegram_gw import Gateway, build_bot, is_owner


def format_ls(tasks: list[Task]) -> str:
    if not tasks:
        return "No active agents\\."
    lines = [f"`{t.id}` {t.repo} — {t.status}" for t in tasks]
    return "\n".join(lines)


def build_dispatcher(supervisor: Supervisor, config: Config) -> Dispatcher:
    dp = Dispatcher()

    def owner_only(message: Message) -> bool:
        uid = message.from_user.id if message.from_user else None
        return is_owner(uid, config.owner_id)

    @dp.message(Command("spawn"), F.func(owner_only))
    async def _spawn(message: Message, command: CommandObject):
        args = (command.args or "").split(maxsplit=1)
        if len(args) < 2:
            await message.answer("Usage: /spawn <repo> <task>")
            return
        repo, task_text = args[0], args[1]
        tid = await supervisor.spawn(repo, task_text)
        await message.answer(f"Spawned task `{tid}` in {repo}")

    @dp.message(Command("ls"), F.func(owner_only))
    async def _ls(message: Message):
        await message.answer(format_ls(supervisor.list_active()))

    @dp.message(Command("kill"), F.func(owner_only))
    async def _kill(message: Message, command: CommandObject):
        if not command.args or not command.args.strip().isdigit():
            await message.answer("Usage: /kill <id>")
            return
        ok = await supervisor.kill(int(command.args.strip()))
        await message.answer("Killed" if ok else "No such active task")

    @dp.message(Command("panic"), F.func(owner_only))
    async def _panic(message: Message):
        n = await supervisor.panic()
        await message.answer(f"Killed {n} agents")

    return dp


async def main() -> None:
    config = load_config(os.environ)
    registry = Registry.open(os.environ.get("FLEETD_DB", "fleetd.sqlite"))
    bot = build_bot(config)
    gateway = Gateway(bot, config)
    supervisor = Supervisor(config, registry, gateway)
    dp = build_dispatcher(supervisor, config)
    await dp.start_polling(bot)


def run() -> None:
    asyncio.run(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_integration.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: PASS (all tests across all files green)

- [ ] **Step 6: Commit**

```bash
git add src/fleetd/app.py tests/test_integration.py
git commit -m "feat: app wiring, owner-gated commands, end-to-end integration"
```

---

## Self-Review

**Spec coverage (Phase 1 scope):**
- `fleetd` core, asyncio ✔ (app.py) · SQLite registry ✔ (Task 2) · survives-restart via session_id persistence ✔ (registry stores `session_id`; resume-*on-restart* wiring is Phase 3, spec §7).
- Spawn / `/ls` / `/kill` / `/panic` ✔ (Tasks 7–8).
- Per-task topic create + (delete deferred: see note) — **gap addressed below.**
- stream-json → live-edited activity message ✔ (Tasks 3, 4, 7).
- Owner-ID hard lock ✔ (Task 6 `is_owner`, Task 8 `owner_only` on every handler).
- Bot token from env, never committed ✔ (Task 1 `.gitignore`, `load_config`).
- Audit log ✔ (Task 2 + spawn/kill/panic calls).
- Native mode only ✔.

**Gap found & resolved:** topic *deletion* on completion (spec §3.1) is not wired
in Task 7. Deletion belongs with the `[close]` button, which is a callback-query
handler — and inline buttons + callbacks are a Phase-3 "polish" concern (spec §7
lists the done-summary buttons under polish). For Phase 1, completed tasks keep
their topic (status flips to `done`); the topic is closed manually. This is
consistent with the spec's phasing. No code change needed; noted here so the
executor doesn't treat it as missing.

**Placeholder scan:** none — every code step contains complete, runnable code.

**Type consistency:** `Event` fields, `Task` fields, `Registry` method names,
`Gateway` method names, and `Supervisor` constructor signature are used
identically across Tasks 2–8. `activity_line`/`milestone_message` signatures
match between Task 4 and Task 7. `agent_factory` is called with keyword args
(`task_text=`, `cwd=`, `claude_bin=`) consistently in Task 7 and the FakeAgent
lambdas.
