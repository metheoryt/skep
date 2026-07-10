# Sessions A1 — Worker Invocations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the worker's unit of execution an **Invocation** — a resumable, model-selectable run over a multi-root workspace — so one durable Session (owned later by the queen) can have many invocations on the same worktree.

**Architecture:** This is sub-project A of the Sessions design, split along the §3 ownership boundary. **A1 (this plan) is worker-side only** and backward-compatible: every change defaults to today's behavior (single root, no model flag, one invocation per session), so it merges to `main` and is testable at the worker/DB level before the queen-side session registry (A2) exists. The single interface A1 exposes to A2 is one wire field: `task_started` carries `session_local_id`, letting the queen distinguish a first invocation (mint a `ref`) from a resume (reuse it).

**Design note — ref keying (resolved during planning).** The spec §3 says the worker's invocation row is "keyed by session ref". The queen mints `ref` (autoincrement) only *after* `task_started`, and `RemoteWorker.spawn` returns literal `0` — so at first-spawn the worker cannot key by a ref that does not exist. Resolution: the worker owns a **local** session id (`session_local_id`), invocations carry it, and the queen (in A2) maps `ref → (host, profile, session_local_id)` — a natural extension of the existing `Bookkeeping.by_worker_task(host, profile, local_id)`. `session_local_id` of a first invocation equals its own task id; a resume invocation reuses the originating session's id. This honors the spec's *intent* (the ownership split) without inverting the working fire-and-forget spawn protocol. Per CLAUDE.md, when the code and a doc disagree, the code wins; this is that.

**Tech Stack:** Python 3.12+, `sqlite3` (stdlib), `pytest` + `pytest-asyncio`, `mcp.server.fastmcp` (memory shim), `aiohttp` (WS transport). No new dependencies.

## Global Constraints

- **No new third-party dependencies.** Everything here is stdlib + already-vendored (`sqlite3`, `pytest`, `mcp`, `aiohttp`).
- **Backward compatibility is mandatory.** Existing callers (`WorkerWsClient._on_command` → `Supervisor.spawn(repo, task)`, the combined-mode path, every existing test) must keep working unchanged. New capability arrives through new parameters that default to today's behavior and through new methods, never by breaking an existing signature the queen calls over the wire.
- **`--allowedTools` enumeration stays complete and exact** (existing invariant, spec §2.2/§8.1): never a wildcard; skep never relies on a host profile's allowlist surviving the flag.
- **A broken memory store never fails a spawn** (existing invariant, spec §6): read failures log an audit row and omit the addendum; they never propagate.
- **The migration must be tested against a persisted old-schema DB file, never `:memory:`.** `CREATE TABLE IF NOT EXISTS` makes column additions silent no-ops on an existing DB, and an in-memory DB is always built fresh — so an in-memory "migration" test proves nothing. Seed a file, migrate it, assert.
- **Queen-side, Telegram, workspace-name→path resolution, and the `primary:rw` lease are OUT of scope** (they are A2 / C / E). A1 accepts an already-resolved list of local roots and reads `session_local_id`; it never talks to the queen registry or acquires a lease.

---

## File Structure

**New files:**
- `src/skep/workspace.py` — the `Root` / `Workspace` value types: an ordered list of named local roots, each with a mode (`new`/`attach`/`primary`) and access (`rw`/`ro`). Pure data + rendering (`cwd` + `--add-dir` paths) + the `requires_lease` predicate. No I/O, no worktree creation.
- `tests/test_workspace.py` — unit tests for rendering and the lease predicate.
- `tests/test_supervisor_resume.py` — the resume path (new invocation, same worktree, stored `resume_token`, optional new model).

**Modified files:**
- `src/skep/db.py` — Registry schema migration (`PRAGMA user_version`), `session_id`→`resume_token` rename, new `model` + `session_local_id` columns, invocation query methods.
- `src/skep/agent.py` — `AgentProcess` renders `--add-dir`, `--model`, `--resume`.
- `src/skep/supervisor.py` — `spawn` delegates to a workspace-aware `spawn_workspace`; new `resume`; sets `session_local_id`; wires multi-root memory; emits `session_local_id` on `task_started`.
- `src/skep/worker/memory_shim.py` — `memory_shim_server` takes named roots; `remember` gains a `project` argument.
- `src/skep/memory.py` — `write_memory` targets a chosen root; the addendum unions all roots' stores.
- `src/skep/wire.py` — `task_started_msg` gains `session_local_id`.
- `src/skep/transport.py` — `EventSink.task_started` (+ 3 impls) gains `session_local_id`.
- `src/skep/ws_transport.py` — `WsEventSink.task_started` forwards `session_local_id`.
- `tests/test_db.py`, `tests/test_supervisor*.py`, `tests/test_memory*.py`, `tests/worker/test_memory_shim.py`, `tests/test_agent.py` — updated for the renamed column and new signatures.

---

## Task 1: DB migration framework + Registry schema

**Files:**
- Modify: `src/skep/db.py`
- Modify: `src/skep/supervisor.py` (rename references to the `session_id` column)
- Modify: `tests/test_db.py` (rename references; add migration test)
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: nothing (foundational).
- Produces:
  - `Registry.open(path: str) -> Registry` now migrates an existing DB to `SCHEMA_VERSION`.
  - `Task` dataclass: `session_id` field renamed to `resume_token: str | None`; new fields `model: str | None`, `session_local_id: int | None`.
  - `_TASK_COLUMNS` includes `resume_token`, `model`, `session_local_id` (so `Registry.update(...)` accepts them).
  - Module constant `SCHEMA_VERSION: int = 1`.

- [ ] **Step 1: Write the failing migration test**

Add to `tests/test_db.py` (keep existing imports; add `import sqlite3` if absent):

```python
def test_open_migrates_old_schema_file(tmp_path):
    # A pre-migration DB: session_id column, no model/session_local_id, user_version 0.
    db_file = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        CREATE TABLE tasks (
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
        CREATE TABLE audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER, kind TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO tasks (repo, task, worktree_path, session_id, status, created_at)"
        " VALUES ('nix', 't', '/wt/nix-1', 'sess-old', 'done', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    reg = Registry.open(str(db_file))
    task = reg.get_task(1)

    assert task.resume_token == "sess-old"          # renamed from session_id
    assert task.model is None                         # new column, back-filled NULL
    assert task.session_local_id == 1                 # back-filled to own id
    assert reg._conn.execute("PRAGMA user_version").fetchone()[0] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_db.py::test_open_migrates_old_schema_file -v`
Expected: FAIL — `AttributeError: 'Task' object has no attribute 'resume_token'` (or a KeyError building the Task).

- [ ] **Step 3: Implement the schema + migration**

In `src/skep/db.py`, replace `_SCHEMA`, `_TASK_COLUMNS`, the `Task` dataclass, and `Registry.open`, and add the migration function. `_SCHEMA` stays the **baseline (v0)** shape so fresh and existing DBs both flow through the same migration path:

```python
SCHEMA_VERSION = 1

# Baseline (v0) schema. Both fresh and pre-existing DBs are migrated up to
# SCHEMA_VERSION by _migrate(); keeping the baseline here means one code path.
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
    "id",
    "repo",
    "task",
    "worktree_path",
    "resume_token",
    "model",
    "session_local_id",
    "pid",
    "topic_id",
    "mode",
    "status",
    "created_at",
)


@dataclass
class Task:
    id: int | None
    repo: str
    task: str
    worktree_path: str
    resume_token: str | None
    model: str | None
    session_local_id: int | None
    pid: int | None
    topic_id: int | None
    mode: str
    status: str
    created_at: str
```

Replace `Registry.open` and add `_migrate`:

```python
    @classmethod
    def open(cls, path: str) -> Registry:
        conn = sqlite3.connect(path)
        conn.executescript(_SCHEMA)
        conn.commit()
        _migrate(conn)
        return cls(conn)
```

```python
def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        # v0 -> v1: rename session_id -> resume_token; add model +
        # session_local_id; back-fill session_local_id to the row's own id
        # (each existing task becomes a one-invocation session).
        conn.execute("ALTER TABLE tasks RENAME COLUMN session_id TO resume_token")
        conn.execute("ALTER TABLE tasks ADD COLUMN model TEXT")
        conn.execute("ALTER TABLE tasks ADD COLUMN session_local_id INTEGER")
        conn.execute(
            "UPDATE tasks SET session_local_id = id WHERE session_local_id IS NULL"
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
```

Note: `ALTER TABLE ... RENAME COLUMN` requires SQLite ≥ 3.25 (2018); the Python 3.12 stdlib bundles far newer. `_row_to_task`, `get_task`, `list_all`, `list_active`, `update`, `add_task` need no change — they iterate `_TASK_COLUMNS` and whitelist against it.

- [ ] **Step 4: Update the renamed-column references in supervisor.py**

In `src/skep/supervisor.py`, `run_events` references the old field name. Change both:

```python
                if ev.kind == "system" and ev.session_id:
                    self._reg.update(task_id, resume_token=ev.session_id)
                if ev.kind == "result":
                    saw_result = True
                    summary = ev.text
                    self._reg.update(
                        task_id,
                        resume_token=ev.session_id or self._task(task_id).resume_token,
                    )
                    terminal = "failed" if ev.is_error else "done"
```

(`ev.session_id` is the Claude stream `Event.session_id` from `stream.py` — unrelated to the column; it stays.)

- [ ] **Step 5: Update existing test_db references**

In `tests/test_db.py`, any test that passes `session_id=` to `update` or reads `task.session_id` must use `resume_token`. For example `test_update_fields` (existing) — change `reg.update(tid, status="running", pid=999, session_id="sess-1", topic_id=7)` to `resume_token="sess-1"` and assert `task.resume_token == "sess-1"`.

- [ ] **Step 6: Run the DB suite**

Run: `uv run pytest tests/test_db.py -v`
Expected: PASS (migration test + all existing, updated tests).

- [ ] **Step 7: Commit**

```bash
git add src/skep/db.py src/skep/supervisor.py tests/test_db.py
git commit -m "feat(db): schema migration + resume_token/model/session_local_id columns"
```

---

## Task 2: Registry invocation queries

**Files:**
- Modify: `src/skep/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: Task 1's `session_local_id` column and `Task` dataclass.
- Produces:
  - `Registry.list_invocations(session_local_id: int) -> list[Task]` — all invocation rows sharing a session, oldest first.
  - `Registry.latest_invocation(session_local_id: int) -> Task | None` — the newest invocation row for a session (highest `id`), or `None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_db.py`:

```python
def test_invocation_queries_group_by_session():
    reg = Registry.open(":memory:")
    # First invocation of a session: session_local_id == own id.
    a = reg.add_task("nix", "t", "/wt/nix-1")
    reg.update(a, session_local_id=a, resume_token="tok-a")
    # A second invocation (resume) of the SAME session, same worktree.
    b = reg.add_task("nix", "t", "/wt/nix-1")
    reg.update(b, session_local_id=a, resume_token="tok-b")
    # An unrelated session.
    c = reg.add_task("web", "u", "/wt/web-3")
    reg.update(c, session_local_id=c)

    invs = reg.list_invocations(a)
    assert [t.id for t in invs] == [a, b]
    assert reg.latest_invocation(a).id == b
    assert reg.latest_invocation(a).resume_token == "tok-b"
    assert reg.latest_invocation(c).id == c
    assert reg.latest_invocation(999) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_db.py::test_invocation_queries_group_by_session -v`
Expected: FAIL — `AttributeError: 'Registry' object has no attribute 'list_invocations'`.

- [ ] **Step 3: Implement the queries**

Add to `Registry` in `src/skep/db.py` (next to `list_active`):

```python
    def list_invocations(self, session_local_id: int) -> list[Task]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE session_local_id = ? ORDER BY id",
            (session_local_id,),
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def latest_invocation(self, session_local_id: int) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE session_local_id = ? ORDER BY id DESC LIMIT 1",
            (session_local_id,),
        ).fetchone()
        return self._row_to_task(row) if row else None
```

- [ ] **Step 4: Run the test**

Run: `uv run pytest tests/test_db.py::test_invocation_queries_group_by_session -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/skep/db.py tests/test_db.py
git commit -m "feat(db): invocation grouping queries (list/latest by session)"
```

---

## Task 3: AgentProcess renders --add-dir, --model, --resume

**Files:**
- Modify: `src/skep/agent.py`
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `AgentProcess.__init__` gains three keyword params (all default to today's behavior):
  - `add_dirs: list[Path] | None = None` → renders `--add-dir <p>` per extra root.
  - `model: str | None = None` → renders `--model <model>`.
  - `resume_token: str | None = None` → renders `--resume <token>`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_agent.py`:

```python
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
    assert "--add-dir" in argv
    # Each extra root gets its own --add-dir flag.
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
```

Ensure `from pathlib import Path` is imported in the test module (it likely is; add if not).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent.py::test_argv_renders_add_dir_model_resume -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'add_dirs'`.

- [ ] **Step 3: Implement**

In `src/skep/agent.py`, extend `AgentProcess.__init__` (add params after `append_system_prompt`) and store them:

```python
    def __init__(
        self,
        task_text: str,
        cwd: Path,
        claude_bin: str,
        config_dir: str | None = None,
        mcp_servers: dict[str, dict] | None = None,
        allowed_tools: list[str] | None = None,
        append_system_prompt: str | None = None,
        add_dirs: list[Path] | None = None,
        model: str | None = None,
        resume_token: str | None = None,
    ) -> None:
        self._task_text = task_text
        self._cwd = cwd
        self._claude_bin = claude_bin
        self._config_dir = config_dir
        self._mcp_servers = mcp_servers
        self._allowed_tools = allowed_tools
        self._append_system_prompt = append_system_prompt
        self._add_dirs = add_dirs
        self._model = model
        self._resume_token = resume_token
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr: bytes = b""
        self._stderr_task: asyncio.Task | None = None
```

In `_argv`, render the flags after the `--allowedTools` block (before `return argv`):

```python
        if self._add_dirs:
            for d in self._add_dirs:
                argv += ["--add-dir", str(d)]
        if self._model is not None:
            argv += ["--model", self._model]
        if self._resume_token is not None:
            argv += ["--resume", self._resume_token]
        return argv
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_agent.py -v`
Expected: PASS (new tests + existing argv tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/skep/agent.py tests/test_agent.py
git commit -m "feat(agent): render --add-dir, --model, --resume in argv"
```

---

## Task 4: Workspace value type

**Files:**
- Create: `src/skep/workspace.py`
- Test: `tests/test_workspace.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Root(name: str, path: Path, mode: str = "new", access: str = "rw", attach_ref: str | None = None)` — frozen dataclass.
  - Mode constants `MODE_NEW = "new"`, `MODE_ATTACH = "attach"`, `MODE_PRIMARY = "primary"`; access constants `ACCESS_RW = "rw"`, `ACCESS_RO = "ro"`.
  - `Workspace(roots: list[Root])` — frozen dataclass; `__post_init__` raises `ValueError` if `roots` is empty.
  - `Workspace.primary_path -> Path` — `roots[0].path` (the eventual `cwd`; for a `new` root the caller substitutes the created worktree — see Task 5).
  - `Workspace.add_dir_paths -> list[Path]` — `[r.path for r in roots[1:]]`.
  - `Workspace.requires_lease -> bool` — `True` iff any root is `primary` + `rw`.
  - `Workspace.single(name: str, path: Path) -> Workspace` — classmethod building today's one-root `new`/`rw` default.

- [ ] **Step 1: Write the failing test**

Create `tests/test_workspace.py`:

```python
from pathlib import Path

import pytest

from skep.workspace import (
    ACCESS_RO,
    MODE_NEW,
    MODE_PRIMARY,
    Root,
    Workspace,
)


def test_single_root_default_is_new_rw():
    ws = Workspace.single("nix", Path("/repos/nix"))
    assert ws.roots[0].mode == MODE_NEW
    assert ws.roots[0].access == "rw"
    assert ws.add_dir_paths == []
    assert ws.requires_lease is False
    assert ws.primary_path == Path("/repos/nix")


def test_multi_root_renders_cwd_and_add_dirs():
    ws = Workspace(
        roots=[
            Root("nix", Path("/wt/nix-1"), mode=MODE_NEW),
            Root("main", Path("/repos/main"), mode=MODE_PRIMARY, access=ACCESS_RO),
        ]
    )
    assert ws.primary_path == Path("/wt/nix-1")
    assert ws.add_dir_paths == [Path("/repos/main")]
    # primary:ro needs no lease.
    assert ws.requires_lease is False


def test_primary_rw_requires_lease():
    ws = Workspace(roots=[Root("main", Path("/repos/main"), mode=MODE_PRIMARY)])
    assert ws.requires_lease is True


def test_empty_workspace_rejected():
    with pytest.raises(ValueError):
        Workspace(roots=[])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_workspace.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'skep.workspace'`.

- [ ] **Step 3: Implement**

Create `src/skep/workspace.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

MODE_NEW = "new"
MODE_ATTACH = "attach"
MODE_PRIMARY = "primary"

ACCESS_RW = "rw"
ACCESS_RO = "ro"


@dataclass(frozen=True)
class Root:
    """One directory a session operates in.

    `mode` is how the session relates to the directory:
    - new: create and own a fresh worktree (today's behavior; the default).
    - attach: join an existing (shared) worktree at `path`; `attach_ref` names it.
    - primary: operate in the repo's main checkout at `path`.

    `access` is orthogonal: rw may write, ro is read-only (advisory in A1 —
    real enforcement is Phase 4). A1 never resolves names: `path` is already
    a concrete local path (C's job, upstream).
    """

    name: str
    path: Path
    mode: str = MODE_NEW
    access: str = ACCESS_RW
    attach_ref: str | None = None


@dataclass(frozen=True)
class Workspace:
    roots: list[Root] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.roots:
            raise ValueError("a workspace needs at least one root")

    @classmethod
    def single(cls, name: str, path: Path) -> Workspace:
        """Today's default: one own worktree, read-write."""
        return cls(roots=[Root(name, path, mode=MODE_NEW, access=ACCESS_RW)])

    @property
    def primary_path(self) -> Path:
        return self.roots[0].path

    @property
    def add_dir_paths(self) -> list[Path]:
        return [r.path for r in self.roots[1:]]

    @property
    def requires_lease(self) -> bool:
        # An exclusive queen-held lease is needed exactly when a non-owned root
        # is opened rw — i.e. primary:rw (spec §6). Enforcement is A2; A1 only
        # reports the requirement.
        return any(
            r.mode == MODE_PRIMARY and r.access == ACCESS_RW for r in self.roots
        )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_workspace.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/skep/workspace.py tests/test_workspace.py
git commit -m "feat(workspace): Root/Workspace value types with lease predicate"
```

---

## Task 5: Supervisor.spawn_workspace (multi-root, model, session_local_id)

**Files:**
- Modify: `src/skep/supervisor.py`
- Test: `tests/test_supervisor.py`

**Interfaces:**
- Consumes: `Workspace`/`Root` (Task 4); `AgentProcess` add_dirs/model params (Task 3); `session_local_id` column (Task 1).
- Produces:
  - `Supervisor.spawn(self, repo: str, task: str) -> int` — unchanged signature (protocol/wire compatibility); now a thin wrapper that builds `Workspace.single(repo, repos_root/repo)` and calls `spawn_workspace`.
  - `Supervisor.spawn_workspace(self, workspace: Workspace, task: str, *, model: str | None = None) -> int` — the engine. Prepares each root (creates a worktree only for a `new` root[0]; uses `path` as-is for `attach`/`primary`), inserts an invocation with `session_local_id = tid` (first invocation of a new session), renders `cwd` + `--add-dir` + `--model`, spawns.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_supervisor.py`. Use the existing test's fakes (an `AgentProcess` stub via `agent_factory` and a no-op `worktree_factory`); mirror whatever fixture/stub pattern the file already uses. A representative test:

```python
@pytest.mark.asyncio
async def test_spawn_workspace_renders_multi_root_and_sets_session_local_id(
    worker_config, fake_sink
):
    created = {}

    def fake_agent(**kwargs):
        created.update(kwargs)
        return FakeAgent()  # existing stub in this test module

    reg = Registry.open(":memory:")
    sup = Supervisor(
        worker_config,
        reg,
        fake_sink,
        agent_factory=fake_agent,
        worktree_factory=lambda *a: None,
    )
    ws = Workspace(
        roots=[
            Root("nix", worker_config.repos_root / "nix", mode=MODE_NEW),
            Root(
                "main",
                worker_config.repos_root / "main",
                mode=MODE_PRIMARY,
                access=ACCESS_RO,
            ),
        ]
    )
    tid = await sup.spawn_workspace(ws, "do the thing", model="claude-sonnet-5")

    task = reg.get_task(tid)
    assert task.session_local_id == tid          # first invocation keys to itself
    assert task.model == "claude-sonnet-5"
    # cwd is the created worktree for the new root[0]; the primary root is an add-dir.
    assert created["add_dirs"] == [worker_config.repos_root / "main"]
    assert created["model"] == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_spawn_is_single_root_workspace(worker_config, fake_sink):
    # The legacy entrypoint still works and produces one new/rw root.
    reg = Registry.open(":memory:")
    created = {}

    def fake_agent(**kwargs):
        created.update(kwargs)
        return FakeAgent()

    sup = Supervisor(
        worker_config, reg, fake_sink,
        agent_factory=fake_agent, worktree_factory=lambda *a: None,
    )
    tid = await sup.spawn("nix", "t")
    task = reg.get_task(tid)
    assert task.session_local_id == tid
    assert task.model is None
    assert created.get("add_dirs") in (None, [])
```

Adapt fixture names (`worker_config`, `fake_sink`, `FakeAgent`) to the actual ones in `tests/test_supervisor.py`; add imports `from skep.workspace import Workspace, Root, MODE_NEW, MODE_PRIMARY, ACCESS_RO` and `from skep.db import Registry`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_supervisor.py::test_spawn_workspace_renders_multi_root_and_sets_session_local_id -v`
Expected: FAIL — `AttributeError: 'Supervisor' object has no attribute 'spawn_workspace'`.

- [ ] **Step 3: Implement**

In `src/skep/supervisor.py`, add the import at the top:

```python
from skep.workspace import MODE_NEW, Workspace
```

Replace the existing `spawn` method with a wrapper plus the new engine. The engine keeps the existing try/except teardown structure verbatim — only the pre-spawn setup (worktree, invocation row, agent kwargs) changes:

```python
    async def spawn(self, repo: str, task: str) -> int:
        # Legacy / wire entrypoint: one own worktree, read-write, no model.
        ws = Workspace.single(repo, self._cfg.repos_root / repo)
        return await self.spawn_workspace(ws, task)

    async def spawn_workspace(
        self, workspace: Workspace, task: str, *, model: str | None = None
    ) -> int:
        if len(self._agents) >= self._cfg.max_concurrent:
            raise CapacityError(f"at capacity ({self._cfg.max_concurrent} running)")

        head = workspace.roots[0]
        tid = self._reg.add_task(head.name, task, "", mode="native")
        # First invocation of a new session keys to its own id (see plan preamble).
        self._reg.update(tid, session_local_id=tid, model=model)

        # Resolve each root to a concrete on-disk path; create a worktree only
        # for a `new` head root (today's behavior). attach/primary roots are
        # used as-is (their path is already the shared/main checkout).
        if head.mode == MODE_NEW:
            branch = f"skep/{_slug(task)}-{tid}"
            head_path = self._cfg.worktrees_root / f"{head.name}-{tid}"
        else:
            branch = None
            head_path = head.path
        self._reg.update(tid, worktree_path=str(head_path))
        add_dirs = list(workspace.add_dir_paths)

        agent: AgentProcess | None = None
        shim: MailboxShim | None = None
        try:
            if head.mode == MODE_NEW:
                self._worktree_factory(head.path, head_path, branch)
            self._reg.log_audit(tid, "spawn", f"{head.name}: {task}")

            agent_kwargs: dict[str, Any] = dict(
                task_text=task,
                cwd=head_path,
                claude_bin=self._cfg.claude_bin,
                config_dir=self._cfg.claude_config_dir,
                add_dirs=add_dirs,
                model=model,
            )
            mcp_servers: dict[str, dict] = {}
            allowed_tools: list[str] = list(BASE_TOOLS)

            if self._cfg.memory_enabled:
                roots = [(r.name, r.path) for r in workspace.roots]
                if self._memory is not None:
                    try:
                        addendum = await self._memory.addendum_for(
                            [p for _, p in roots]
                        )
                    except Exception as exc:
                        self._reg.log_audit(tid, "error", f"memory read failed: {exc}")
                        addendum = None
                    if addendum is not None:
                        agent_kwargs["append_system_prompt"] = addendum
                mcp_servers["memory"] = memory_shim_server(roots)
                allowed_tools += MEMORY_TOOLS

            if self._mailbox_client is not None:
                token = secrets.token_urlsafe(32)
                shim = self._shim_factory(self._mailbox_client, tid, token=token)
                mcp_url = await shim.start()
                server: dict[str, object] = {"type": "http", "url": mcp_url}
                server["headers"] = {"Authorization": f"Bearer {token}"}
                mcp_servers["mailbox"] = server
                allowed_tools += MAILBOX_TOOLS

            if mcp_servers:
                agent_kwargs["mcp_servers"] = mcp_servers
            agent_kwargs["allowed_tools"] = allowed_tools

            agent = self._agent_factory(**agent_kwargs)
            await agent.start()
            self._agents[tid] = agent
            if shim is not None:
                self._shims[tid] = shim
            self._reg.update(tid, status="running", pid=agent.pid)

            await self._sink.task_started(tid, head.name, task, tid)
            t = asyncio.create_task(self.run_events(tid, agent))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        except Exception as exc:
            self._agents.pop(tid, None)
            self._shims.pop(tid, None)
            self._reg.update(tid, status="failed")
            self._reg.log_audit(tid, "error", f"spawn failed: {exc}")
            if agent is not None:
                try:
                    await agent.kill()
                except Exception as kill_exc:
                    self._reg.log_audit(
                        tid, "error", f"agent kill failed on spawn error: {kill_exc}"
                    )
            if shim is not None:
                try:
                    await shim.stop()
                except Exception as stop_exc:
                    self._reg.log_audit(
                        tid,
                        "error",
                        f"mailbox shim stop failed on spawn error: {stop_exc}",
                    )
            raise

        return tid
```

Notes for the implementer:
- `self._sink.task_started(tid, head.name, task, tid)` — the 4th arg is `session_local_id`, equal to `tid` for a first invocation. The `EventSink` signature gains this parameter in Task 8; **this line will not type-check / run until Task 8 lands**, so if you run the supervisor suite between Task 5 and Task 8, expect a `TypeError` on that call. Either implement Task 8 before running the full supervisor suite, or temporarily assert only through `spawn_workspace`'s return + registry state with a sink stub that already accepts 4 args (the `fake_sink` in the test above should accept `session_local_id`).
- `memory_shim_server(roots)` and `addendum_for([paths])` change signature in Task 7; the same ordering caveat applies. To keep this task independently runnable, the test above uses `agent_factory`/`worktree_factory` stubs and can run with `memory_enabled=False` (set it in `worker_config`) so the memory calls are skipped. Prefer that for Task 5's own test; Task 7 covers the memory path.

- [ ] **Step 4: Run the supervisor suite**

Run: `uv run pytest tests/test_supervisor.py -v`
Expected: PASS. If `fake_sink` in existing tests does not yet accept the 4th `task_started` arg, update that stub now (it is the same change Task 8 formalizes) or run Task 8 first.

- [ ] **Step 5: Commit**

```bash
git add src/skep/supervisor.py tests/test_supervisor.py
git commit -m "feat(supervisor): workspace-aware spawn with model and session_local_id"
```

---

## Task 6: Supervisor.resume (new invocation, same worktree)

**Files:**
- Modify: `src/skep/supervisor.py`
- Create: `tests/test_supervisor_resume.py`

**Interfaces:**
- Consumes: `Registry.latest_invocation` (Task 2); `AgentProcess.resume_token`/`model` (Task 3); `spawn_workspace` internals (Task 5).
- Produces:
  - `Supervisor.resume(self, session_local_id: int, *, model: str | None = None) -> int` — finds the session's latest invocation, reuses its `worktree_path`, inserts a new invocation row (`session_local_id` = the same session), spawns an `AgentProcess` with `resume_token` and optional `model`. Raises `ValueError` if the session is unknown or has no `resume_token` to resume from. Returns the new invocation's task id.

- [ ] **Step 1: Write the failing test**

Create `tests/test_supervisor_resume.py`:

```python
import pytest

from skep.db import Registry
from skep.supervisor import Supervisor


@pytest.mark.asyncio
async def test_resume_starts_new_invocation_same_worktree(worker_config, fake_sink):
    created = {}

    def fake_agent(**kwargs):
        created.update(kwargs)
        return FakeAgent()  # reuse the stub from tests/test_supervisor.py

    reg = Registry.open(":memory:")
    # Seed a finished first invocation with a harvested resume_token.
    first = reg.add_task("nix", "t", "/wt/nix-1")
    reg.update(first, session_local_id=first, resume_token="tok-1", status="done")

    sup = Supervisor(
        worker_config, reg, fake_sink,
        agent_factory=fake_agent, worktree_factory=lambda *a: None,
    )
    # worker_config.memory_enabled must be False for this unit test.
    second = await sup.resume(first, model="claude-opus-4-8")

    task = reg.get_task(second)
    assert second != first
    assert task.session_local_id == first          # same session
    assert task.worktree_path == "/wt/nix-1"        # same worktree, no new one
    assert created["resume_token"] == "tok-1"
    assert created["model"] == "claude-opus-4-8"
    # No worktree was created on resume.
    assert created["cwd"].as_posix().endswith("/wt/nix-1")


@pytest.mark.asyncio
async def test_resume_unknown_session_raises(worker_config, fake_sink):
    reg = Registry.open(":memory:")
    sup = Supervisor(worker_config, reg, fake_sink, worktree_factory=lambda *a: None)
    with pytest.raises(ValueError):
        await sup.resume(12345)
```

Import/adapt `FakeAgent`, `worker_config`, `fake_sink` from the shared test fixtures. If `FakeAgent` lives in `tests/test_supervisor.py`, move it to `tests/conftest.py` as a fixture-returning stub during this task, and update `tests/test_supervisor.py`'s import — commit that refactor as part of this task.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_supervisor_resume.py -v`
Expected: FAIL — `AttributeError: 'Supervisor' object has no attribute 'resume'`.

- [ ] **Step 3: Implement**

Add to `Supervisor` in `src/skep/supervisor.py`:

```python
    async def resume(
        self, session_local_id: int, *, model: str | None = None
    ) -> int:
        if len(self._agents) >= self._cfg.max_concurrent:
            raise CapacityError(f"at capacity ({self._cfg.max_concurrent} running)")
        prev = self._reg.latest_invocation(session_local_id)
        if prev is None:
            raise ValueError(f"no such session: {session_local_id}")
        if prev.resume_token is None:
            raise ValueError(
                f"session {session_local_id} has no resume_token to resume from"
            )

        worktree_path = Path(prev.worktree_path)
        tid = self._reg.add_task(prev.repo, prev.task, prev.worktree_path, mode="native")
        # A resume is a NEW invocation of the SAME session.
        self._reg.update(tid, session_local_id=session_local_id, model=model)

        agent: AgentProcess | None = None
        shim: MailboxShim | None = None
        try:
            self._reg.log_audit(tid, "resume", f"resume session {session_local_id}")
            agent_kwargs: dict[str, Any] = dict(
                task_text=prev.task,
                cwd=worktree_path,
                claude_bin=self._cfg.claude_bin,
                config_dir=self._cfg.claude_config_dir,
                model=model,
                resume_token=prev.resume_token,
            )
            allowed_tools: list[str] = list(BASE_TOOLS)
            agent_kwargs["allowed_tools"] = allowed_tools

            agent = self._agent_factory(**agent_kwargs)
            await agent.start()
            self._agents[tid] = agent
            self._reg.update(tid, status="running", pid=agent.pid)

            await self._sink.task_started(tid, prev.repo, prev.task, session_local_id)
            t = asyncio.create_task(self.run_events(tid, agent))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        except Exception as exc:
            self._agents.pop(tid, None)
            self._shims.pop(tid, None)
            self._reg.update(tid, status="failed")
            self._reg.log_audit(tid, "error", f"resume failed: {exc}")
            if agent is not None:
                try:
                    await agent.kill()
                except Exception as kill_exc:
                    self._reg.log_audit(
                        tid, "error", f"agent kill failed on resume error: {kill_exc}"
                    )
            raise

        return tid
```

Add `from pathlib import Path` to the imports if not already present (it is — `Supervisor` already imports `Path`).

Note: v1 resume rehydrates memory/mailbox wiring is intentionally minimal — the resume invocation carries `resume_token` + `model` and the base coding tools; memory and mailbox re-attachment on resume is a follow-up (it needs the same workspace roots stored on the session, which A2 supplies). Keep A1's resume focused on the verified `--resume`/`--model` mechanics. The `task_started` 4th arg here is `session_local_id` (the original), which is how the queen (A2) recognizes this as a resume, not a new session.

- [ ] **Step 4: Run the resume test**

Run: `uv run pytest tests/test_supervisor_resume.py -v`
Expected: PASS (requires Task 8's `task_started` 4-arg sink, or a `fake_sink` stub already accepting it).

- [ ] **Step 5: Commit**

```bash
git add src/skep/supervisor.py tests/test_supervisor_resume.py tests/conftest.py tests/test_supervisor.py
git commit -m "feat(supervisor): resume a session as a new invocation on the same worktree"
```

---

## Task 7: Multi-root memory (project-targeted write, unioned read)

**Files:**
- Modify: `src/skep/memory.py`
- Modify: `src/skep/worker/memory_shim.py`
- Test: `tests/test_memory.py`, `tests/worker/test_memory_shim.py`

**Interfaces:**
- Consumes: the roots list built in Task 5 (`[(name, path), ...]`).
- Produces:
  - `memory.write_memory(root_paths: dict[str, Path], project: str | None, title, body, kind="gotcha", supersedes=None, now=None) -> Path` — writes into the named project's `.agent-memory/`, defaulting to the first root. **First parameter changes** from a single `repo_path: Path` to `root_paths: dict[str, Path]` + a `project` selector. `now` keeps its existing type `Callable[[], datetime] | None`.
  - `memory.MemoryStore.addendum_for(root_paths: list[Path]) -> str | None` — unions facts across all roots. **Signature changes** from a single `repo_path` to a list.
  - `memory.MemoryProbe.addendum_for(root_paths: list[Path]) -> str | None` — the protocol matches (so `Supervisor`'s injected `MemoryProbe` and every test fake update together).
  - `memory_shim.memory_shim_server(roots: list[tuple[str, Path]]) -> dict[str, object]` — passes `name=path` pairs to the shim child.
  - `memory_shim`'s `remember(title, body, kind="gotcha", supersedes=None, project: str | None = None) -> str` — `project` selects the target root by name; default is the first root.

- [ ] **Step 1: Write the failing tests**

The existing `test_memory.py` / `test_memory_write.py` / `test_memory_shim.py` call `write_memory(repo_path, ...)` and `addendum_for(repo_path)`. First, add the new-behavior tests, then (Step 5) migrate the existing callers.

Add to `tests/test_memory.py`:

```python
@pytest.mark.asyncio
async def test_addendum_unions_roots(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    (a / ".agent-memory").mkdir(parents=True)
    (b / ".agent-memory").mkdir(parents=True)
    write_memory({"a": a}, "a", "fact in A", "body a", "gotcha")
    write_memory({"b": b}, "b", "fact in B", "body b", "gotcha")

    store = MemoryStore()
    addendum = await store.addendum_for([a, b])
    assert "fact in A" in addendum
    assert "fact in B" in addendum


def test_write_memory_targets_named_project(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    (a / ".agent-memory").mkdir(parents=True)
    (b / ".agent-memory").mkdir(parents=True)
    roots = {"a": a, "b": b}
    p_default = write_memory(roots, None, "goes to first", "x", "gotcha")
    p_named = write_memory(roots, "b", "goes to b", "y", "gotcha")
    assert (a / ".agent-memory") in p_default.parents
    assert (b / ".agent-memory") in p_named.parents


def test_write_memory_unknown_project_raises(tmp_path):
    a = tmp_path / "a"
    (a / ".agent-memory").mkdir(parents=True)
    with pytest.raises(ValueError):
        write_memory({"a": a}, "nope", "t", "b", "gotcha")
```

Add to `tests/worker/test_memory_shim.py`:

```python
def test_remember_writes_to_named_project(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    (a / ".agent-memory").mkdir(parents=True)
    (b / ".agent-memory").mkdir(parents=True)
    remember = build_remember({"a": a, "b": b})
    path = remember("title", "body", project="b")
    assert str(b / ".agent-memory") in path
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_memory.py::test_addendum_unions_roots tests/worker/test_memory_shim.py::test_remember_writes_to_named_project -v`
Expected: FAIL — `write_memory` / `addendum_for` / `build_remember` reject the new argument shapes.

- [ ] **Step 3: Implement memory.py**

In `src/skep/memory.py`, change `write_memory`'s first parameter to a `dict[str, Path]` of project-name → repo path plus a `project` selector, resolve the target repo path from it, then run the **existing body verbatim** (it already operates on a local `repo_path` variable — slug/containment/supersedes logic is unchanged). Only the first few lines are new:

```python
def write_memory(
    root_paths: dict[str, Path],
    project: str | None,
    title: str,
    body: str,
    kind: str = "gotcha",
    supersedes: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> Path:
    if project is None:
        # Default target is the first root (insertion-ordered dict).
        repo_path = next(iter(root_paths.values()))
    elif project in root_paths:
        repo_path = root_paths[project]
    else:
        raise ValueError(f"unknown project: {project!r} (have {list(root_paths)})")
    # ---- everything below is the existing body, unchanged ----
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind!r}")
    clock = now or _utcnow
    root = memory_dir(repo_path)
    # ... rest of the existing write_memory body ...
```

(`Callable` is already imported in `memory.py`.) Change `MemoryStore.addendum_for` to accept a list of roots and union their facts. `_load` already filters superseded facts and sorts within a root; the union must re-sort so cross-root ordering by `created` holds:

```python
    async def addendum_for(self, root_paths: list[Path]) -> str | None:
        facts: list[MemoryFact] = []
        for repo_path in root_paths:
            facts.extend(self._load(repo_path))
        # _load sorts within a root; re-sort the union so newest-first holds
        # across roots too.
        facts.sort(key=lambda f: f.created, reverse=True)
        return self.render(facts)
```

Update the `MemoryProbe` protocol at the bottom of the file to match:

```python
class MemoryProbe(Protocol):
    """What Supervisor needs from memory: an addendum, or None."""

    async def addendum_for(self, root_paths: list[Path]) -> str | None: ...
```

Keep the byte-budget in `render` unchanged. No dedupe across roots: facts from different repos are genuinely distinct even if two slugs coincide.

- [ ] **Step 4: Implement memory_shim.py**

In `src/skep/worker/memory_shim.py`, change `memory_shim_server` to pass `name=path` pairs, `main()` to parse them into a dict, and `build_remember` to close over the dict and accept `project`:

```python
def memory_shim_server(roots: list[tuple[str, Path]]) -> dict[str, object]:
    """The `--mcp-config` entry; each root passed as name=path (name has no '=')."""
    args = ["-m", "skep.worker.memory_shim"]
    args += [f"{name}={path}" for name, path in roots]
    return {"type": "stdio", "command": sys.executable, "args": args}


def build_remember(root_paths: dict[str, Path]) -> Callable[..., str]:
    def remember(
        title: str,
        body: str,
        kind: str = "gotcha",
        supersedes: str | None = None,
        project: str | None = None,
    ) -> str:
        """Record a durable fact about a project in this workspace for future agents.

        project: which workspace root to write to (by name); defaults to the
        primary root. kind: one of gotcha, constraint, decision, convention,
        incident. Returns the path of the written memory file.
        """
        return str(write_memory(root_paths, project, title, body, kind, supersedes))

    return remember


def build_server(root_paths: dict[str, Path]) -> FastMCP:
    server = FastMCP("memory")
    server.tool()(build_remember(root_paths))
    return server


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m skep.worker.memory_shim <name>=<path> ...")
    roots: dict[str, Path] = {}
    for arg in sys.argv[1:]:
        name, _, path = arg.partition("=")
        roots[name] = Path(path)
    build_server(roots).run(transport="stdio")
```

- [ ] **Step 5: Migrate the existing memory callers and tests**

- `tests/test_memory.py`, `tests/test_memory_write.py`: existing calls `write_memory(repo, title, ...)` → `write_memory({"repo": repo}, None, title, ...)`; `addendum_for(repo)` → `addendum_for([repo])`.
- `tests/worker/test_memory_shim.py`: `build_remember(repo)` → `build_remember({"repo": repo})`; `memory_shim_server(repo)` → `memory_shim_server([("repo", repo)])`.
- `tests/test_supervisor_memory.py` and `tests/test_agent_memory.py`: update any direct `addendum_for` / `memory_shim_server` expectations to the new shapes.
- No production caller besides `Supervisor.spawn_workspace` (already updated in Task 5) uses these.

- [ ] **Step 6: Run the memory suites**

Run: `uv run pytest tests/test_memory.py tests/test_memory_write.py tests/worker/test_memory_shim.py tests/test_supervisor_memory.py tests/test_agent_memory.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/skep/memory.py src/skep/worker/memory_shim.py tests/test_memory.py tests/test_memory_write.py tests/worker/test_memory_shim.py tests/test_supervisor_memory.py tests/test_agent_memory.py
git commit -m "feat(memory): multi-root workspace — project-targeted write, unioned read"
```

---

## Task 8: task_started carries session_local_id (the A1→A2 interface)

**Files:**
- Modify: `src/skep/wire.py`
- Modify: `src/skep/transport.py`
- Modify: `src/skep/ws_transport.py`
- Test: `tests/test_transport.py`, `tests/test_ws_transport.py`

**Interfaces:**
- Consumes: `Supervisor` emits the 4th arg (Task 5/6).
- Produces:
  - `wire.task_started_msg(local_id: int, repo: str, title: str, session_local_id: int) -> dict`.
  - `EventSink.task_started(self, local_id: int, repo: str, title: str, session_local_id: int) -> None` — protocol + `InMemoryEventSink`, `SwitchableEventSink`, `WsEventSink`.
  - The queen (`QueenWsServer._dispatch`, `on_task_started`, register-replay) is intentionally **not** changed — it keeps reading `local_id`/`repo`/`title` and ignores `session_local_id` until A2. The field rides the wire unused.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_transport.py` (adapt to the file's existing recording-sink pattern):

```python
@pytest.mark.asyncio
async def test_task_started_carries_session_local_id():
    recorded = {}

    class RecordingInbox:
        async def on_task_started(self, host, profile, local_id, repo, title):
            recorded.update(
                host=host, profile=profile, local_id=local_id, repo=repo, title=title
            )
        # other QueenInbox methods can be no-ops / omitted if the sink only calls this

    sink = InMemoryEventSink(RecordingInbox(), "h1", "default")
    # 4-arg call is the new contract; the in-memory queen path drops the field.
    await sink.task_started(7, "nix", "t", 7)
    assert recorded["local_id"] == 7
```

Add to `tests/test_ws_transport.py` a check that the wire frame includes the field:

```python
def test_task_started_msg_includes_session_local_id():
    msg = wire.task_started_msg(7, "nix", "t", 3)
    assert msg["local_id"] == 7
    assert msg["session_local_id"] == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ws_transport.py::test_task_started_msg_includes_session_local_id tests/test_transport.py::test_task_started_carries_session_local_id -v`
Expected: FAIL — `task_started_msg()` / `task_started()` take too few arguments.

- [ ] **Step 3: Implement wire.py**

```python
def task_started_msg(
    local_id: int, repo: str, title: str, session_local_id: int
) -> dict[str, Any]:
    return {
        "t": TASK_STARTED,
        "local_id": local_id,
        "repo": repo,
        "title": title,
        "session_local_id": session_local_id,
    }
```

- [ ] **Step 4: Implement transport.py (protocol + 3 sinks)**

`EventSink` protocol:

```python
    async def task_started(
        self, local_id: int, repo: str, title: str, session_local_id: int
    ) -> None: ...
```

`InMemoryEventSink.task_started` — accept and drop `session_local_id` (the combined/in-memory queen path threads it in A2; today `QueenInbox.on_task_started` has no such field):

```python
    async def task_started(
        self, local_id: int, repo: str, title: str, session_local_id: int
    ) -> None:
        # session_local_id is A2's; the in-memory queen ignores it for now.
        await self._inbox.on_task_started(
            self._host, self._profile, local_id, repo, title
        )
```

`SwitchableEventSink.task_started` — forward all four:

```python
    async def task_started(
        self, local_id: int, repo: str, title: str, session_local_id: int
    ) -> None:
        if self.target is not None:
            await self.target.task_started(local_id, repo, title, session_local_id)
```

- [ ] **Step 5: Implement ws_transport.py (WsEventSink)**

```python
    async def task_started(
        self, local_id: int, repo: str, title: str, session_local_id: int
    ) -> None:
        await self._send(
            wire.task_started_msg(local_id, repo, title, session_local_id)
        )
```

The queen's `_dispatch`/`on_task_started` and the register-replay loop stay as they are — they read `msg["local_id"]`, `msg["repo"]`, `msg["title"]` and ignore the extra field. Do **not** change `QueenInbox`, `_active_payload`, or the replay in this task; that is A2's to consume.

- [ ] **Step 6: Run the transport suites and the full test run**

Run: `uv run pytest tests/test_transport.py tests/test_ws_transport.py -v`
Expected: PASS.

Then the whole suite (all prior tasks converge here — this is where the supervisor's 4-arg `task_started` call becomes valid):

Run: `uv run pytest -q`
Expected: PASS. Fix any remaining test stub that constructs an `EventSink` and still defines a 3-arg `task_started` (update it to 4 args).

- [ ] **Step 7: Commit**

```bash
git add src/skep/wire.py src/skep/transport.py src/skep/ws_transport.py tests/test_transport.py tests/test_ws_transport.py
git commit -m "feat(transport): task_started carries session_local_id (A1->A2 interface)"
```

---

## Self-Review

**Spec coverage (against `docs/superpowers/specs/2026-07-10-sessions-design.md`):**
- §2 Invocation (resume_token + model per run) → Tasks 1, 2, 3, 6. ✅
- §2 naming collision `session_id`→`resume_token` → Task 1. ✅
- §4 lifecycle parked→running as a new invocation on the same worktree → Task 6 (the worker mechanics; the queen state machine is A2). ✅
- §5 model per invocation, verified `--model` on resume → Tasks 3, 6. ✅
- §6 workspace as ordered roots → `cwd` + `--add-dir`; per-root mode/access; `requires_lease` only for `primary:rw` → Tasks 3, 4, 5. ✅ (lease *acquisition* is A2.)
- §8 visibility → **A2** (queen-owned); not in A1. Noted out of scope. ✅
- §11 shipped-code changes: Registry rename + model (T1), Invocation keying (T1/T2/T5/T6), `Supervisor.spawn` resolved workspace (T5), `memory_shim` project arg (T7). ✅ Bookkeeping session-scoping + lease table + workspace store are **A2/C** — correctly deferred.
- §13 testing: state machine (A2), invocation keying (T2/T5/T6), workspace rendering + lease predicate (T4), `remember` targeting + unioned read (T7), migration from existing rows (T1). ✅ Lease acquire/release and visibility tests are A2.

**Placeholder scan:** No TBD/TODO; every code step shows complete code. The two "adapt to existing fixture names" notes (Tasks 5, 6) point at real, named fixtures the implementer will see in the open test file — not vague instructions.

**Type consistency:** `session_local_id: int` everywhere (column, `task_started` 4th arg, wire field). `resume_token: str | None` consistent (column, `Task`, `AgentProcess.resume_token`, `latest_invocation().resume_token`). `write_memory(root_paths: dict[str, Path], project: str | None, ...)` and `addendum_for(root_paths: list[Path])` — the dict-vs-list split is intentional (write targets one named root; read unions a path list) and consistently applied in Tasks 5 and 7. `Workspace.single`/`Root` names match between Task 4's definition and Task 5's use.

**Cross-task ordering caveat (called out so a subagent doesn't trip):** Tasks 5 and 6 emit the 4-arg `task_started`, which only becomes valid when Task 8 lands. Each of those tasks' own tests avoids the dependency by using a sink stub that already accepts 4 args (and `memory_enabled=False` for Task 5's unit test). The full-suite green gate is at the end of Task 8. If executing strictly in order, keep the interim sink stubs 4-arg from Task 5 onward.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-10-sessions-a1-worker-invocations.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
