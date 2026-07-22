# Sessions A2 — queen-side sessions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the queen a session identity that survives across invocations, and make
A1's multi-root spawn drivable from Telegram through one concrete behavior — an agent
working in its own worktree while watching the repo's main checkout read-only.

**Architecture:** Three independent seams. (1) `Bookkeeping` gains
`session_local_id` plus a migration, so a `ref` and its Telegram topic can be reused by a
later invocation of the same session. (2) The spawn wire frame gains a `roots` list
carrying **names only**; the worker resolves names to paths under its own `repos_root`,
which is what keeps `--add-dir` from becoming an arbitrary-filesystem-read primitive
driven by the queen. (3) `/spawn … --watch` composes the two-root workspace, and the
worker's own write paths (memory shim, spawn addendum) learn to respect `access="ro"`.

**Tech Stack:** Python 3.12+, `uv`, `pytest` (asyncio auto mode), `aiohttp` (WS),
`sqlite3` (stdlib), `aiogram` (Telegram). Types checked with `uvx ty check src`,
lint with `uvx ruff check`.

**Spec:** `docs/superpowers/specs/2026-07-22-sessions-a2-queen-sessions-design.md`.
Parent spec: `docs/superpowers/specs/2026-07-10-sessions-design.md`.

## Global Constraints

- `src` must stay clean under `uvx ty check src` (0 errors). Annotate new functions;
  the repo's ruff config has `ANN` enabled.
- Full suite command: `uv run pytest -q -m "not mdns"`. Baseline before this plan:
  **350 passed, 3 skipped, 1 deselected**. Every task ends with the suite green.
- Never put a filesystem path in a wire frame. Roots cross the wire as names.
- `attach` mode and `primary` + `rw` are refused in A2 — raise, never silently downgrade.
- The head root (index 0) is always `mode="new"`. This preserves the invariant that
  `.skep/mcp.json` lives under a tid-unique worktree (`supervisor.py` documents why a
  persistent head root would need a tid-keyed filename first).
- `ro` is advisory for the agent (it has `Bash`); it is **binding** on skep's own write
  paths. Real enforcement is Phase 4.
- Commit after every task. Conventional-commit prefixes (`feat:`, `test:`, `refactor:`).

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/skep/queen/bookkeeping.py` | session column, migration, `by_session`, `rebind_invocation` | 1 |
| `src/skep/queen/telegram_sink.py` | ref/topic reuse on a known session | 2 |
| `src/skep/transport.py` | `QueenInbox` / `EventSink` / `CommandHandler` signatures | 2, 6 |
| `src/skep/wire.py` | `session_local_id` on the register payload, `roots` on spawn | 3, 6 |
| `src/skep/ws_transport.py` | replay threading, `RemoteWorker.spawn`, `_on_command` | 3, 6 |
| `src/skep/worker/roots.py` | **new** — name→path resolution and its refusals | 4 |
| `src/skep/supervisor.py` | `spawn(roots=…)`, rw-only shim roots, prompt composition | 5, 7 |
| `src/skep/workspace.py` | `readonly_declaration()` | 7 |
| `src/skep/queen/router.py` | `cmd_spawn(roots=…)` | 6 |
| `src/skep/app.py` | `--watch` parsing, dispatcher wiring | 8 |

---

### Task 1: Bookkeeping learns sessions

**Files:**
- Modify: `src/skep/queen/bookkeeping.py`
- Test: `tests/test_bookkeeping.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Entry.session_local_id: int | None`
  - `Bookkeeping.add(host, profile, local_id, repo, title, topic_id, session_local_id: int | None = None) -> int`
  - `Bookkeeping.by_session(host: str, profile: str, session_local_id: int) -> Entry | None`
  - `Bookkeeping.rebind_invocation(ref: int, local_id: int) -> None`

**Note on the migration idiom:** `_SCHEMA` stays the **v0 baseline** — do not add the
new column to it. A fresh DB is created at v0 and then migrated up, so there is exactly
one code path. This mirrors `src/skep/db.py`, which A1 established.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bookkeeping.py`:

```python
import sqlite3


def test_add_defaults_session_local_id_to_local_id():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("g16", "work", 5, "nix", "t", topic_id=1)
    assert bk.get(ref).session_local_id == 5


def test_add_stores_explicit_session_local_id():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("g16", "work", 9, "nix", "t", topic_id=1, session_local_id=5)
    assert bk.get(ref).session_local_id == 5


def test_by_session_finds_the_row():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("g16", "work", 5, "nix", "t", topic_id=1)
    assert bk.by_session("g16", "work", 5).ref == ref
    assert bk.by_session("g16", "personal", 5) is None
    assert bk.by_session("g16", "work", 99) is None


def test_rebind_invocation_repoints_local_id_and_reactivates():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("g16", "work", 5, "nix", "t", topic_id=1)
    bk.set_status(ref, "done")

    bk.rebind_invocation(ref, 9)

    e = bk.get(ref)
    assert e.local_id == 9
    assert e.status == "running"
    assert e.session_local_id == 5      # the session id never moves
    assert e.topic_id == 1              # the topic never moves
    assert bk.by_worker_task("g16", "work", 9).ref == ref
    assert bk.by_worker_task("g16", "work", 5) is None


def test_migration_backfills_existing_rows(tmp_path):
    # A v0 database written by the shipped code, then opened by this version.
    path = str(tmp_path / "bk.sqlite")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE entries (
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
    )
    conn.execute(
        "INSERT INTO entries (host, profile, local_id, repo, title, topic_id)"
        " VALUES ('g16', 'work', 7, 'nix', 'old task', 42)"
    )
    conn.commit()
    conn.close()

    bk = Bookkeeping.open(path)
    e = bk.by_worker_task("g16", "work", 7)
    assert e.session_local_id == 7      # one-invocation session
    assert e.topic_id == 42
    bk.close()

    # Re-opening an already-migrated DB must be a no-op, not an error.
    bk2 = Bookkeeping.open(path)
    assert bk2.by_worker_task("g16", "work", 7).session_local_id == 7
    bk2.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_bookkeeping.py -q`
Expected: FAIL — `TypeError: Entry.__init__() got an unexpected keyword argument` or
`AttributeError: 'Bookkeeping' object has no attribute 'by_session'`.

- [ ] **Step 3: Implement**

In `src/skep/queen/bookkeeping.py`, add the version constant above `_SCHEMA`:

```python
SCHEMA_VERSION = 1
```

Leave `_SCHEMA` exactly as it is (v0 baseline). Add `"session_local_id"` to `_COLUMNS`
after `"local_id"`, and the matching field to `Entry`:

```python
_COLUMNS = (
    "ref",
    "host",
    "profile",
    "local_id",
    "session_local_id",
    "repo",
    "title",
    "topic_id",
    "activity_msg_id",
    "status",
)


@dataclass
class Entry:
    ref: int
    host: str
    profile: str
    local_id: int
    session_local_id: int | None
    repo: str
    title: str
    topic_id: int
    activity_msg_id: int | None
    status: str
```

Add the migration function after `_COLUMNS`:

```python
def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        # v0 -> v1: entries become session-scoped. Every existing row is a
        # one-invocation session, so its session id is its own local_id.
        conn.execute("ALTER TABLE entries ADD COLUMN session_local_id INTEGER")
        conn.execute(
            "UPDATE entries SET session_local_id = local_id"
            " WHERE session_local_id IS NULL"
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
```

Call it from `open`:

```python
    @classmethod
    def open(cls, path: str) -> Bookkeeping:
        conn = sqlite3.connect(path)
        conn.executescript(_SCHEMA)
        conn.commit()
        _migrate(conn)
        return cls(conn)
```

Replace `add` and append the two new methods:

```python
    def add(
        self,
        host: str,
        profile: str,
        local_id: int,
        repo: str,
        title: str,
        topic_id: int,
        session_local_id: int | None = None,
    ) -> int:
        # A first invocation is its own session (mirrors the worker-side rule
        # in Supervisor.spawn_workspace).
        sid = local_id if session_local_id is None else session_local_id
        cur = self._conn.execute(
            "INSERT INTO entries (host, profile, local_id, session_local_id, repo,"
            " title, topic_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'running')",
            (host, profile, local_id, sid, repo, title, topic_id),
        )
        self._conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def by_session(
        self, host: str, profile: str, session_local_id: int
    ) -> Entry | None:
        row = self._conn.execute(
            "SELECT * FROM entries WHERE host=? AND profile=? AND session_local_id=?"
            " ORDER BY ref DESC LIMIT 1",
            (host, profile, session_local_id),
        ).fetchone()
        return self._row(row) if row else None

    def rebind_invocation(self, ref: int, local_id: int) -> None:
        """Point an existing session's row at a new invocation.

        The ref, the topic and the session id all stay put -- that is what
        makes a topic follow a session across invocations.
        """
        self._conn.execute(
            "UPDATE entries SET local_id=?, status='running' WHERE ref=?",
            (local_id, ref),
        )
        self._conn.commit()
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_bookkeeping.py -q`
Expected: PASS.

Then the full suite: `uv run pytest -q -m "not mdns"`
Expected: `350 passed` — the `Entry` field addition is keyword-constructed via
`_COLUMNS`, so no existing call site breaks.

- [ ] **Step 5: Commit**

```bash
git add src/skep/queen/bookkeeping.py tests/test_bookkeeping.py
git commit -m "feat(queen): bookkeeping entries become session-scoped"
```

---

### Task 2: The sink reuses a known session's ref and topic

**Files:**
- Modify: `src/skep/transport.py` (the `QueenInbox` protocol and `InMemoryEventSink`)
- Modify: `src/skep/queen/telegram_sink.py:22-28`
- Test: `tests/test_telegram_sink.py`

**Interfaces:**
- Consumes: `Bookkeeping.by_session`, `Bookkeeping.rebind_invocation`,
  `Bookkeeping.add(..., session_local_id=…)` from Task 1.
- Produces: `QueenInbox.on_task_started(host, profile, local_id, repo, title, session_local_id: int | None = None)`.

**Read before implementing:** the reuse branch must run **before** `create_topic`. Today
the topic is created first and its id passed into `add()`; reusing a topic means never
calling the gateway at all.

**Honest status:** nothing in A2 calls this branch — only a resume produces a second
`task_started` for a known session, and resume is out of scope. It is tested directly.
Do not add a fake caller to "exercise" it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_telegram_sink.py`:

```python
async def test_second_invocation_reuses_ref_and_topic():
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)

    await sink.on_task_started("g16", "work", 5, "nix", "t", session_local_id=5)
    ref = bk.by_worker_task("g16", "work", 5).ref
    await sink.on_done("g16", "work", 5, "done", "")

    # A resume: new invocation id, same session.
    await sink.on_task_started("g16", "work", 9, "nix", "t", session_local_id=5)

    gw.create_topic.assert_awaited_once()          # NOT a second topic
    e = bk.by_worker_task("g16", "work", 9)
    assert e.ref == ref
    assert e.topic_id == 555
    assert e.status == "running"


async def test_unknown_session_creates_a_new_topic():
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)
    await sink.on_task_started("g16", "work", 5, "nix", "t", session_local_id=5)
    await sink.on_task_started("g16", "work", 9, "nix", "t", session_local_id=9)
    assert gw.create_topic.await_count == 2


async def test_task_started_without_session_id_behaves_as_before():
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)
    await sink.on_task_started("g16", "work", 5, "nix", "t")
    gw.create_topic.assert_awaited_once_with("g16·work·nix")
    assert bk.by_worker_task("g16", "work", 5).session_local_id == 5


async def test_reattach_of_the_same_invocation_is_still_idempotent():
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)
    await sink.on_task_started("g16", "work", 5, "nix", "t", session_local_id=5)
    await sink.on_task_started("g16", "work", 5, "nix", "t", session_local_id=5)
    gw.create_topic.assert_awaited_once()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_telegram_sink.py -q`
Expected: FAIL — `TypeError: on_task_started() got an unexpected keyword argument 'session_local_id'`.

- [ ] **Step 3: Implement**

In `src/skep/transport.py`, widen the protocol method:

```python
    async def on_task_started(
        self,
        host: str,
        profile: str,
        local_id: int,
        repo: str,
        title: str,
        session_local_id: int | None = None,
    ) -> None: ...
```

In the same file, `InMemoryEventSink.task_started` currently drops the field. Forward it
and delete the now-stale comment:

```python
    async def task_started(
        self, local_id: int, repo: str, title: str, session_local_id: int | None = None
    ) -> None:
        await self._inbox.on_task_started(
            self._host, self._profile, local_id, repo, title, session_local_id
        )
```

In `src/skep/queen/telegram_sink.py`:

```python
    async def on_task_started(
        self,
        host: str,
        profile: str,
        local_id: int,
        repo: str,
        title: str,
        session_local_id: int | None = None,
    ) -> None:
        if self._bk.by_worker_task(host, profile, local_id) is not None:
            return  # re-attach: worker re-registered an already-known invocation
        if session_local_id is not None:
            prior = self._bk.by_session(host, profile, session_local_id)
            if prior is not None:
                # A later invocation of a known session: the topic follows the
                # session, so reuse it -- and never create a second one.
                self._bk.rebind_invocation(prior.ref, local_id)
                return
        topic_id = await self._gw.create_topic(f"{host}·{profile}·{repo}")
        self._bk.add(
            host,
            profile,
            local_id,
            repo,
            title,
            topic_id,
            session_local_id=session_local_id,
        )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_telegram_sink.py tests/test_transport.py -q`
Expected: PASS.

Then: `uv run pytest -q -m "not mdns"` — expected `350 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/skep/transport.py src/skep/queen/telegram_sink.py tests/test_telegram_sink.py
git commit -m "feat(queen): topic follows session across invocations"
```

---

### Task 3: `session_local_id` survives reconnect replay

**Files:**
- Modify: `src/skep/wire.py:31-51` (`register_msg`, `heartbeat_msg` — payload docs only)
- Modify: `src/skep/ws_transport.py:100-112` (replay loop), `:136-146` (dispatch), `:293-301` (`_active_payload`)
- Test: `tests/test_ws_transport.py`

**Interfaces:**
- Consumes: `QueenInbox.on_task_started(..., session_local_id=…)` from Task 2.
- Produces: each dict in the register/heartbeat `active_tasks` list gains a
  `session_local_id: int | None` key.

**Why:** replay is currently idempotent (Task 2 keeps it that way), so this changes no
behavior today. It closes the gap the A1 whole-branch review flagged: the moment resume
exists, replayed rows would otherwise be sessionless and mint fresh refs.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ws_transport.py` (follow the existing fixtures in that file for
building a client/server pair; the payload assertions below are the new content):

```python
def test_active_payload_carries_session_local_id(tmp_path):
    from skep.db import Registry
    from skep.ws_transport import WorkerWsClient

    reg = Registry.open(":memory:")
    tid = reg.add_task("nix", "t", str(tmp_path))
    reg.update(tid, session_local_id=tid, status="running")

    sup = SimpleNamespace(list_active=reg.list_active)
    # NOTE the real parameter names: WorkerWsClient(config=, supervisor=, switch=).
    client = WorkerWsClient(
        config=_worker_cfg(), supervisor=sup, switch=SwitchableEventSink()
    )

    payload = client._active_payload()
    assert payload[0]["local_id"] == tid
    assert payload[0]["session_local_id"] == tid


def _server(inbox):
    # QueenWsServer's positional order is (router, inbox, secret).
    return QueenWsServer(QueenRouter(Bookkeeping.open(":memory:")), inbox, "s")


async def test_replay_passes_session_local_id_to_the_inbox():
    inbox = AsyncMock()
    await _server(inbox)._replay_active(
        "g16", "work", [{"local_id": 9, "repo": "nix", "title": "t", "session_local_id": 5}]
    )
    inbox.on_task_started.assert_awaited_once_with("g16", "work", 9, "nix", "t", 5)


async def test_replay_tolerates_a_missing_session_local_id():
    inbox = AsyncMock()
    await _server(inbox)._replay_active(
        "g16", "work", [{"local_id": 9, "repo": "nix", "title": "t"}]
    )
    inbox.on_task_started.assert_awaited_once_with("g16", "work", 9, "nix", "t", None)
```

These tests call `_replay_active`, which Step 3 extracts from `QueenWsServer._handle`.
Test the extracted method rather than duplicating the replay loop in the test.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ws_transport.py -q`
Expected: FAIL — `KeyError: 'session_local_id'` / the inbox called with 5 positional args.

- [ ] **Step 3: Implement**

In `src/skep/ws_transport.py`, `_active_payload`:

```python
    def _active_payload(self) -> list[dict[str, Any]]:
        return [
            {
                "local_id": t.id,
                "repo": t.repo,
                "title": t.task,
                "session_local_id": t.session_local_id,
            }
            for t in self._sup.list_active()
            if t.id is not None
        ]
```

Extract the replay loop out of `_handle` into a method, and pass the field through:

```python
    async def _replay_active(
        self, host: str, profile: str, active_tasks: list[dict[str, Any]]
    ) -> None:
        for t in active_tasks:
            try:
                sid = t.get("session_local_id")
                await self._inbox.on_task_started(
                    host,
                    profile,
                    int(t["local_id"]),
                    str(t["repo"]),
                    str(t["title"]),
                    None if sid is None else int(sid),
                )
            except Exception:
                logger.exception(
                    "error replaying active task from %s/%s: %r", host, profile, t
                )
                continue
```

and in `_handle` replace the inline loop with:

```python
            await self._replay_active(host, profile, list(reg.get("active_tasks", [])))
```

In `_dispatch`, the `TASK_STARTED` branch already reads the frame; make sure it forwards
`msg.get("session_local_id")` to `on_task_started` as the sixth argument.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_ws_transport.py -q` — expected PASS.
Then: `uv run pytest -q -m "not mdns"` — expected `350 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/skep/ws_transport.py tests/test_ws_transport.py
git commit -m "feat(transport): carry session_local_id through register replay"
```

---

### Task 4: Root resolution on the worker

**Files:**
- Create: `src/skep/worker/roots.py`
- Test: `tests/worker/test_roots.py`

**Interfaces:**
- Consumes: `Root`, `Workspace`, `MODE_NEW`, `MODE_PRIMARY`, `MODE_ATTACH`, `ACCESS_RW`,
  `ACCESS_RO` from `src/skep/workspace.py`.
- Produces:
  - `class RootError(ValueError)`
  - `resolve_roots(repos_root: Path, specs: list[dict[str, Any]]) -> Workspace`

**This function is the security gate.** It takes `repos_root` rather than the whole
config so it has one dependency and is trivially testable. Every refusal raises
`RootError`; none downgrades silently.

- [ ] **Step 1: Write the failing tests**

Create `tests/worker/test_roots.py`:

```python
from pathlib import Path

import pytest

from skep.worker.roots import RootError, resolve_roots

REPOS = Path("/repos")


def test_single_new_root_resolves_under_repos_root():
    ws = resolve_roots(REPOS, [{"name": "nix", "mode": "new", "access": "rw"}])
    assert ws.roots[0].path == REPOS / "nix"
    assert ws.roots[0].mode == "new"
    assert ws.roots[0].access == "rw"
    assert ws.add_dir_paths == []


def test_watch_pattern_resolves_to_two_roots():
    ws = resolve_roots(
        REPOS,
        [
            {"name": "nix", "mode": "new", "access": "rw"},
            {"name": "nix", "mode": "primary", "access": "ro"},
        ],
    )
    assert ws.primary_path == REPOS / "nix"
    assert ws.add_dir_paths == [REPOS / "nix"]
    assert ws.requires_lease is False


def test_mode_and_access_default_when_omitted():
    ws = resolve_roots(REPOS, [{"name": "nix"}])
    assert (ws.roots[0].mode, ws.roots[0].access) == ("new", "rw")


@pytest.mark.parametrize(
    "name",
    ["../etc", "a/b", "/etc", "..", "nix/../..", "a\\b", ""],
)
def test_names_that_could_escape_repos_root_are_refused(name):
    with pytest.raises(RootError):
        resolve_roots(REPOS, [{"name": name}])


def test_primary_rw_is_refused_pending_the_lease():
    with pytest.raises(RootError, match="lease"):
        resolve_roots(
            REPOS,
            [
                {"name": "nix", "mode": "new", "access": "rw"},
                {"name": "nix", "mode": "primary", "access": "rw"},
            ],
        )


def test_attach_is_refused():
    with pytest.raises(RootError, match="attach"):
        resolve_roots(
            REPOS,
            [
                {"name": "nix", "mode": "new", "access": "rw"},
                {"name": "other", "mode": "attach", "access": "rw"},
            ],
        )


def test_head_root_must_be_new():
    with pytest.raises(RootError, match="head"):
        resolve_roots(REPOS, [{"name": "nix", "mode": "primary", "access": "ro"}])


def test_unknown_mode_or_access_is_refused():
    with pytest.raises(RootError):
        resolve_roots(REPOS, [{"name": "nix", "mode": "teleport"}])
    with pytest.raises(RootError):
        resolve_roots(REPOS, [{"name": "nix", "access": "wx"}])


def test_empty_spec_list_is_refused():
    with pytest.raises(RootError):
        resolve_roots(REPOS, [])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/worker/test_roots.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'skep.worker.roots'`.

- [ ] **Step 3: Implement**

Create `src/skep/worker/roots.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

from skep.workspace import (
    ACCESS_RO,
    ACCESS_RW,
    MODE_ATTACH,
    MODE_NEW,
    MODE_PRIMARY,
    Root,
    Workspace,
)


class RootError(ValueError):
    """A root spec the worker refuses to resolve.

    Every refusal is explicit: a silent downgrade (dropping a root, or opening
    it read-only when rw was asked for) would let the queen believe a session
    has access it does not have.
    """


# `attach` is deliberately absent: there is no shared-worktree registry yet,
# so nothing can validate an attach_ref.
_MODES = (MODE_NEW, MODE_PRIMARY)
_ACCESS = (ACCESS_RW, ACCESS_RO)


def _resolve_name(repos_root: Path, name: object) -> Path:
    """Map a repo NAME to a path under repos_root, refusing anything else.

    Names cross the wire; paths never do. `--add-dir` is an arbitrary-read
    primitive, so a name that can escape repos_root would hand a rogue queen
    the contents of the worker's disk (spec section 4).
    """
    if not isinstance(name, str) or not name:
        raise RootError(f"root name must be a non-empty string, got {name!r}")
    if "/" in name or "\\" in name or name == ".." or name.startswith("."):
        raise RootError(f"root name may not contain a path: {name!r}")
    resolved = (repos_root / name).resolve()
    if repos_root.resolve() not in resolved.parents:
        raise RootError(f"root {name!r} escapes {repos_root}")
    return repos_root / name


def resolve_roots(repos_root: Path, specs: list[dict[str, Any]]) -> Workspace:
    if not specs:
        raise RootError("a workspace needs at least one root")

    roots: list[Root] = []
    for i, spec in enumerate(specs):
        name = spec.get("name")
        mode = spec.get("mode", MODE_NEW)
        access = spec.get("access", ACCESS_RW)

        if mode == MODE_ATTACH:
            raise RootError("attach roots are not supported yet")
        if mode not in _MODES:
            raise RootError(f"unknown root mode: {mode!r}")
        if access not in _ACCESS:
            raise RootError(f"unknown root access: {access!r}")
        if mode == MODE_PRIMARY and access == ACCESS_RW:
            raise RootError(
                "primary:rw needs a queen-held lease, which is not built yet"
            )
        if i == 0 and mode != MODE_NEW:
            # The head root becomes the agent's cwd and holds .skep/mcp.json,
            # whose filename is not tid-keyed yet -- a persistent head root
            # would let concurrent agents clobber each other's token file.
            raise RootError("the head root must be mode 'new'")

        roots.append(
            Root(str(name), _resolve_name(repos_root, name), mode=mode, access=access)
        )

    return Workspace(roots=roots)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/worker/test_roots.py -q` — expected PASS (11 tests).
Then: `uvx ty check src` — expected 0 errors.

- [ ] **Step 5: Commit**

```bash
git add src/skep/worker/roots.py tests/worker/test_roots.py
git commit -m "feat(worker): resolve root names to paths, fail closed"
```

---

### Task 5: `Supervisor.spawn` accepts root specs

**Files:**
- Modify: `src/skep/supervisor.py:84-88`
- Test: `tests/test_supervisor.py`

**Interfaces:**
- Consumes: `resolve_roots`, `RootError` from Task 4.
- Produces: `Supervisor.spawn(repo: str, task: str, roots: list[dict[str, Any]] | None = None) -> int`.

**Why here:** `Supervisor` is the in-process `CommandHandler`, so putting resolution in
`spawn` gives both the WS path and the single-process path the same behavior from one
call site.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_supervisor.py` (reuse the module's existing supervisor fixture and
fake agent factory; the assertions below are the new content):

```python
async def test_spawn_with_roots_renders_add_dir(sup, agent_factory, cfg):
    await sup.spawn(
        "nix",
        "clean up",
        roots=[
            {"name": "nix", "mode": "new", "access": "rw"},
            {"name": "nix", "mode": "primary", "access": "ro"},
        ],
    )
    kwargs = agent_factory.calls[-1]
    assert kwargs["add_dirs"] == [cfg.repos_root / "nix"]
    # cwd is still the fresh worktree, not the primary checkout
    assert kwargs["cwd"].parent == cfg.worktrees_root


async def test_spawn_without_roots_is_unchanged(sup, agent_factory, cfg):
    await sup.spawn("nix", "clean up")
    kwargs = agent_factory.calls[-1]
    assert kwargs["add_dirs"] == []
    assert kwargs["cwd"].parent == cfg.worktrees_root


async def test_spawn_with_a_refused_root_raises_root_error(sup):
    from skep.worker.roots import RootError

    with pytest.raises(RootError):
        await sup.spawn(
            "nix",
            "t",
            roots=[
                {"name": "nix", "mode": "new", "access": "rw"},
                {"name": "../etc", "mode": "primary", "access": "ro"},
            ],
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_supervisor.py -q`
Expected: FAIL — `TypeError: spawn() got an unexpected keyword argument 'roots'`.

- [ ] **Step 3: Implement**

In `src/skep/supervisor.py`, add the import:

```python
from skep.worker.roots import resolve_roots
```

and replace `spawn`:

```python
    async def spawn(
        self, repo: str, task: str, roots: list[dict[str, Any]] | None = None
    ) -> int:
        # `roots` carries NAMES from the queen; the worker owns name->path
        # resolution so no path ever crosses the wire. Absent roots is the
        # legacy shape: one own worktree, read-write, no model.
        if roots is None:
            ws = Workspace.single(repo, self._cfg.repos_root / repo)
        else:
            ws = resolve_roots(self._cfg.repos_root, roots)
        return await self.spawn_workspace(ws, task)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_supervisor.py -q` — expected PASS.
Then: `uv run pytest -q -m "not mdns"` — expected `350 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/skep/supervisor.py tests/test_supervisor.py
git commit -m "feat(worker): Supervisor.spawn accepts root specs"
```

---

### Task 6: `roots` crosses the wire

**Files:**
- Modify: `src/skep/wire.py:81-83` (`spawn_msg`)
- Modify: `src/skep/transport.py` (`CommandHandler.spawn`)
- Modify: `src/skep/ws_transport.py:38-40` (`RemoteWorker.spawn`), `:376-386` (`_on_command`)
- Modify: `src/skep/queen/router.py:62-66` (`cmd_spawn`)
- Test: `tests/test_wire.py`, `tests/test_router.py`, `tests/test_worker_app.py`

**Interfaces:**
- Consumes: `Supervisor.spawn(roots=…)` and `RootError` from Tasks 4-5.
- Produces:
  - `wire.spawn_msg(repo, task, roots: list[dict[str, Any]] | None = None)`
  - `CommandHandler.spawn(repo, task, roots=None) -> int`
  - `QueenRouter.cmd_spawn(host, profile, repo, task, roots=None) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_wire.py`:

```python
def test_spawn_msg_carries_roots():
    roots = [
        {"name": "nix", "mode": "new", "access": "rw"},
        {"name": "nix", "mode": "primary", "access": "ro"},
    ]
    msg = wire.decode(wire.encode(wire.spawn_msg("nix", "t", roots)))
    assert msg["roots"] == roots


def test_spawn_msg_roots_default_to_none():
    msg = wire.decode(wire.encode(wire.spawn_msg("nix", "t")))
    assert msg.get("roots") is None
```

Append to `tests/test_router.py`:

```python
async def test_cmd_spawn_forwards_roots():
    bk = Bookkeeping.open(":memory:")
    router = QueenRouter(bk)
    handler = AsyncMock()
    router.register("g16", "work", handler)
    roots = [{"name": "nix", "mode": "new", "access": "rw"}]

    await router.cmd_spawn("g16", "work", "nix", "t", roots=roots)

    handler.spawn.assert_awaited_once_with("nix", "t", roots)
```

Append to `tests/test_worker_app.py` (the module that exercises `_on_command`):

```python
async def test_on_command_forwards_roots_to_the_supervisor():
    sup = AsyncMock()
    client = _client(sup)
    ws = AsyncMock()
    roots = [{"name": "nix", "mode": "new", "access": "rw"}]

    await client._on_command(ws, wire.spawn_msg("nix", "t", roots))

    sup.spawn.assert_awaited_once_with("nix", "t", roots=roots)


async def test_a_refused_root_is_reported_as_a_spawn_rejection():
    from skep.worker.roots import RootError

    sup = AsyncMock()
    sup.spawn.side_effect = RootError("attach roots are not supported yet")
    client = _client(sup)
    ws = AsyncMock()

    await client._on_command(ws, wire.spawn_msg("nix", "t", [{"name": "nix"}]))

    sent = wire.decode(ws.send_str.await_args[0][0])
    assert sent["t"] == wire.SPAWN_REJECTED
    assert "attach" in sent["reason"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_wire.py tests/test_router.py tests/test_worker_app.py -q`
Expected: FAIL — `TypeError: spawn_msg() takes 2 positional arguments but 3 were given`.

- [ ] **Step 3: Implement**

`src/skep/wire.py`:

```python
def spawn_msg(
    repo: str, task: str, roots: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    # `roots` carries names only -- never paths (spec section 4).
    return {"t": SPAWN, "repo": repo, "task": task, "roots": roots}
```

`src/skep/transport.py`, in the `CommandHandler` protocol:

```python
    async def spawn(
        self, repo: str, task: str, roots: list[dict[str, Any]] | None = None
    ) -> int: ...
```

`src/skep/ws_transport.py`, `RemoteWorker.spawn`:

```python
    async def spawn(
        self, repo: str, task: str, roots: list[dict[str, Any]] | None = None
    ) -> int:
        await self._ws.send_str(wire.encode(wire.spawn_msg(repo, task, roots)))
        return 0
```

`src/skep/ws_transport.py`, the `SPAWN` branch of `_on_command` — add `RootError` to the
import list at the top of the file and to the caught exceptions:

```python
        if t == wire.SPAWN:
            try:
                await self._sup.spawn(
                    str(msg["repo"]), str(msg["task"]), roots=msg.get("roots")
                )
            except (CapacityError, RootError) as exc:
                await ws.send_str(wire.encode(wire.spawn_rejected_msg(str(exc))))
```

`src/skep/queen/router.py`:

```python
    async def cmd_spawn(
        self,
        host: str,
        profile: str,
        repo: str,
        task: str,
        roots: list[dict[str, Any]] | None = None,
    ) -> None:
        handler = self._workers.get((host, profile))
        if handler is None:
            raise UnknownWorker(f"{host}/{profile}")
        await handler.spawn(repo, task, roots)
```

(add `from typing import Any` to `router.py`.)

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_wire.py tests/test_router.py tests/test_worker_app.py -q`
Expected: PASS.
Then: `uv run pytest -q -m "not mdns"` and `uvx ty check src` — expected `350 passed`,
0 type errors.

- [ ] **Step 5: Commit**

```bash
git add src/skep/wire.py src/skep/transport.py src/skep/ws_transport.py src/skep/queen/router.py tests/
git commit -m "feat(transport): carry root specs on the spawn frame"
```

---

### Task 7: Read-only roots bind skep's own write paths

**Files:**
- Modify: `src/skep/workspace.py` (add `readonly_declaration`)
- Modify: `src/skep/supervisor.py:130-152` (shim roots + prompt composition)
- Test: `tests/test_workspace.py`, `tests/test_supervisor_memory.py`

**Interfaces:**
- Consumes: `Workspace`, `Root`, `ACCESS_RO`, `ACCESS_RW`.
- Produces: `readonly_declaration(workspace: Workspace) -> str | None`.

**The trap:** `supervisor.py:137` builds one `roots` variable that feeds **both** the
addendum read (`:141`) and the memory shim (`:151`). The fix is **two lists**. Narrowing
the single variable would silently starve the addendum of the watched root's memory —
exactly the thing the operator asked to see.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workspace.py`:

```python
from skep.workspace import Root, Workspace, readonly_declaration


def test_no_declaration_when_every_root_is_writable():
    ws = Workspace.single("nix", Path("/repos/nix"))
    assert readonly_declaration(ws) is None


def test_declaration_names_each_read_only_root():
    ws = Workspace(
        roots=[
            Root("nix", Path("/wt/nix-1"), mode="new", access="rw"),
            Root("nix", Path("/repos/nix"), mode="primary", access="ro"),
        ]
    )
    text = readonly_declaration(ws)
    assert "/repos/nix" in text
    assert "/wt/nix-1" not in text
    assert "checkout" in text          # branch operations are named
```

Append to `tests/test_supervisor_memory.py`:

```python
async def test_memory_shim_never_receives_a_read_only_root(sup, cfg, mcp_config):
    await sup.spawn(
        "nix",
        "t",
        roots=[
            {"name": "nix", "mode": "new", "access": "rw"},
            {"name": "nix", "mode": "primary", "access": "ro"},
        ],
    )
    servers = mcp_config.written[-1]
    memory_roots = servers["memory"]["args"]
    assert str(cfg.repos_root / "nix") not in " ".join(memory_roots)


async def test_addendum_still_reads_the_read_only_root(sup, cfg, memory, agent_factory):
    memory.seen_roots = []
    await sup.spawn(
        "nix",
        "t",
        roots=[
            {"name": "nix", "mode": "new", "access": "rw"},
            {"name": "nix", "mode": "primary", "access": "ro"},
        ],
    )
    assert cfg.repos_root / "nix" in memory.seen_roots


async def test_prompt_carries_the_read_only_declaration(sup, agent_factory):
    await sup.spawn(
        "nix",
        "t",
        roots=[
            {"name": "nix", "mode": "new", "access": "rw"},
            {"name": "nix", "mode": "primary", "access": "ro"},
        ],
    )
    prompt = agent_factory.calls[-1]["append_system_prompt"]
    assert "READ-ONLY" in prompt


async def test_no_declaration_when_no_read_only_root(sup, agent_factory):
    await sup.spawn("nix", "t")
    prompt = agent_factory.calls[-1].get("append_system_prompt") or ""
    assert "READ-ONLY" not in prompt
```

Adapt the fixture names to the ones `tests/test_supervisor_memory.py` already defines
(it has a fake memory provider and a fake mcp-config writer); do not invent new fixtures
if equivalents exist.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_workspace.py tests/test_supervisor_memory.py -q`
Expected: FAIL — `ImportError: cannot import name 'readonly_declaration'`.

- [ ] **Step 3: Implement**

Append to `src/skep/workspace.py`:

```python
def readonly_declaration(workspace: Workspace) -> str | None:
    """Declare read-only roots to the agent, or None if every root is writable.

    Advisory: the agent has Bash and can ignore this. skep binds its own write
    paths instead (the memory shim takes rw roots only); real enforcement is
    Phase 4's sandbox.
    """
    ro = [r for r in workspace.roots if r.access == ACCESS_RO]
    if not ro:
        return None
    listed = "".join(f"- `{r.path}` ({r.name})\n" for r in ro)
    return (
        "## Read-only roots\n\n"
        "The directories below are READ-ONLY. Read them freely -- that is why "
        "you have them.\n"
        "Do not create, edit or delete files there. Do not run branch "
        "operations (`git checkout`,\n"
        "`git reset`, `git stash`, `git rebase`) in them: another session or "
        "the operator owns\n"
        "that working tree, and switching its branch under them breaks their "
        "work.\n\n"
        f"{listed}"
    )
```

In `src/skep/supervisor.py`, import it:

```python
from skep.workspace import ACCESS_RW, MODE_NEW, Workspace, readonly_declaration
```

(keep the existing names in that import line; add only what is missing.)

Then restructure the prompt/shim section. Before the `if self._cfg.memory_enabled:`
block, add:

```python
            prompt_parts: list[str] = []
```

Inside the memory block, replace the two `roots` uses:

```python
                roots = [(r.name, r.path) for r in workspace.roots]
                # Reads union every root, including ro ones -- reading the
                # watched checkout is the whole point. WRITES are rw-only:
                # `remember(project=...)` selects a root by name, so an ro root
                # in this map would let an agent write .agent-memory/ files
                # into the operator's live checkout.
                write_roots = [
                    (r.name, r.path) for r in workspace.roots if r.access == ACCESS_RW
                ]
                if self._memory is not None:
                    try:
                        addendum = await self._memory.addendum_for(
                            [p for _, p in roots]
                        )
                    except Exception as exc:
                        self._reg.log_audit(tid, "error", f"memory read failed: {exc}")
                        addendum = None
                    if addendum is not None:
                        prompt_parts.append(addendum)
                mcp_servers["memory"] = memory_shim_server(write_roots)
                allowed_tools += MEMORY_TOOLS
```

Note the deleted line: `agent_kwargs["append_system_prompt"] = addendum` no longer
happens here. After the mailbox block, immediately before
`agent_kwargs["allowed_tools"] = allowed_tools`, add:

```python
            declaration = readonly_declaration(workspace)
            if declaration is not None:
                prompt_parts.append(declaration)
            if prompt_parts:
                agent_kwargs["append_system_prompt"] = "\n\n".join(prompt_parts)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_workspace.py tests/test_supervisor_memory.py -q`
Expected: PASS.
Then: `uv run pytest -q -m "not mdns"` — expected `350 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/skep/workspace.py src/skep/supervisor.py tests/test_workspace.py tests/test_supervisor_memory.py
git commit -m "feat(worker): read-only roots bind memory writes and the addendum"
```

---

### Task 8: `/spawn … --watch`

**Files:**
- Modify: `src/skep/app.py:47-64` (`parse_spawn`), `:142-157` (the dispatcher's `/spawn` handler)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `QueenRouter.cmd_spawn(roots=…)` from Task 6.
- Produces: `parse_spawn(args: str) -> tuple[str, str, str, bool, str] | None` —
  `(host, profile, repo, watch, task)`.

**Note the return-type change:** the tuple grows from 4 to 5 elements, with `watch`
before `task`. Existing tests in `tests/test_app.py` that unpack the 4-tuple must be
updated in this task; there is exactly one production call site (`app.py:142`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_app.py`:

```python
def test_parse_spawn_without_watch():
    assert parse_spawn("g16 nix fix the thing") == (
        "g16", "default", "nix", False, "fix the thing",
    )


def test_parse_spawn_with_watch():
    assert parse_spawn("g16 --profile work nix --watch fix the thing") == (
        "g16", "work", "nix", True, "fix the thing",
    )


def test_watch_must_follow_the_repo_not_hide_in_the_task():
    # A --watch that appears later is part of the task text, not a flag.
    host, profile, repo, watch, task = parse_spawn("g16 nix fix --watch the thing")
    assert watch is False
    assert task == "fix --watch the thing"


def test_parse_spawn_rejects_a_watch_with_no_task():
    assert parse_spawn("g16 nix --watch") is None


async def test_spawn_command_with_watch_sends_two_roots(dispatcher, router):
    await _send(dispatcher, "/spawn g16 nix --watch fix it")
    router.cmd_spawn.assert_awaited_once_with(
        "g16",
        "default",
        "nix",
        "fix it",
        roots=[
            {"name": "nix", "mode": "new", "access": "rw"},
            {"name": "nix", "mode": "primary", "access": "ro"},
        ],
    )


async def test_spawn_command_without_watch_sends_no_roots(dispatcher, router):
    await _send(dispatcher, "/spawn g16 nix fix it")
    router.cmd_spawn.assert_awaited_once_with(
        "g16", "default", "nix", "fix it", roots=None
    )
```

Use the dispatcher/router fixtures `tests/test_app.py` already defines for the existing
`/spawn` tests; `_send` stands for whatever helper that file uses to drive a message
through the dispatcher.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_app.py -q`
Expected: FAIL — the 4-tuple does not equal the 5-tuple.

- [ ] **Step 3: Implement**

`src/skep/app.py`:

```python
def parse_spawn(args: str) -> tuple[str, str, str, bool, str] | None:
    """Parse `<host> [--profile <p>] <repo> [--watch] <task...>`.

    Returns (host, profile, repo, watch, task). `--watch` adds the repo's main
    checkout as a read-only second root, so the agent can see uncommitted work
    in the operator's tree while working in its own worktree.
    """
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
    rest = rest[1:]
    watch = False
    if rest and rest[0] == "--watch":
        watch = True
        rest = rest[1:]
    if not rest:
        return None
    task = " ".join(rest)
    return host, profile, repo, watch, task


def watch_roots(repo: str) -> list[dict[str, str]]:
    """The canonical two-root workspace: own worktree + primary checkout, ro."""
    return [
        {"name": repo, "mode": "new", "access": "rw"},
        {"name": repo, "mode": "primary", "access": "ro"},
    ]
```

In the `/spawn` handler:

```python
        parsed = parse_spawn(command.args or "")
        if parsed is None:
            await message.answer(
                "Usage: /spawn <host> [--profile <p>] <repo> [--watch] <task>",
                parse_mode=None,
            )
            return
        host, profile, repo, watch, task = parsed
        try:
            await router.cmd_spawn(
                host, profile, repo, task, roots=watch_roots(repo) if watch else None
            )
        except UnknownWorker:
            await message.answer(f"No worker for {host}/{profile}", parse_mode=None)
            return
        except CapacityError as exc:
            await message.answer(f"Rejected: {exc}", parse_mode=None)
            return
        except RootError as exc:
            await message.answer(f"Rejected: {exc}", parse_mode=None)
            return
        await message.answer(f"Spawned on {host}/{profile}", parse_mode=None)
```

Add `from skep.worker.roots import RootError` to `app.py`'s imports. The `RootError`
branch matters only on the single-process path, where `Supervisor.spawn` raises inline;
over WS the refusal arrives asynchronously as `spawn_rejected`.

Update any existing 4-tuple unpacking in `tests/test_app.py`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_app.py -q` — expected PASS.
Then the full suite plus both gates:

```bash
uv run pytest -q -m "not mdns"
uvx ty check src
uvx ruff check src
```

Expected: `350 passed` plus the new tests, 0 type errors, and no *new* ruff findings
(the repo has 21 pre-existing ones — do not let the count grow).

- [ ] **Step 5: Commit**

```bash
git add src/skep/app.py tests/test_app.py
git commit -m "feat(queen): /spawn --watch adds the primary checkout read-only"
```

---

### Task 9: End-to-end and documentation

**Files:**
- Test: `tests/test_integration.py`
- Modify: `ARCHITECTURE.md`
- Modify: `.claude/memory/project.md`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_integration.py` (follow the file's existing queen+worker
in-process wiring):

```python
async def test_watch_spawn_reaches_the_agent_argv(queen_worker_pair):
    queen, worker, agent_factory, cfg = queen_worker_pair

    await queen.router.cmd_spawn(
        "g16", "work", "nix", "look around", roots=watch_roots("nix")
    )
    await _settle()

    argv = agent_factory.calls[-1]
    assert argv["cwd"].parent == cfg.worktrees_root          # own worktree
    assert argv["add_dirs"] == [cfg.repos_root / "nix"]      # watched checkout
    assert "READ-ONLY" in argv["append_system_prompt"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_integration.py -q`
Expected: FAIL if any seam is unwired; PASS is only meaningful after Tasks 1-8.

- [ ] **Step 3: Make it pass**

No new production code should be required. If it fails, the failure names the unwired
seam — fix that seam rather than adapting the test.

- [ ] **Step 4: Update the docs**

In `ARCHITECTURE.md`, §7 ("you are here"): Sessions A2 moves from *not started* to
*partly shipped* — say which part (registry + the `--watch` slice) and which parts are
still open (`primary:rw` lease, parking/`/resume`, visibility). Fix the branch/commit
stamp at the top of the file. Do not create a dated copy.

In `.claude/memory/project.md`, add one bullet under Decisions recording: the A2 slice
that shipped, the deferrals and their reasons, that the ref/topic-reuse branch has no
live caller until `/resume` exists, and that `/spawn --watch` is opt-in because a watched
checkout exposes uncommitted work.

- [ ] **Step 5: Commit**

```bash
git add tests/test_integration.py ARCHITECTURE.md .claude/memory/project.md
git commit -m "test(sessions): end-to-end watch spawn; refresh architecture and memory"
```

---

## Self-review notes

**Spec coverage.** §3 registry → Tasks 1-3. §4 drive path → Task 6. §5 resolution →
Task 4 (+ Task 5 for the call site). §6 Telegram surface → Task 8. §7 rw-only memory and
the declaration → Task 7. §8 testing → distributed across the tasks it belongs to, plus
Task 9. §9 risks are documented, not implemented — nothing to build.

**Deliberately absent** (spec §2's deferral table): the `primary:rw` lease table,
parking and `/resume`, visibility enforcement, C's catalog, version negotiation. If a
task seems to want one of these, stop and re-read §2 rather than adding it.

**Fixture names** in Tasks 5, 7, 8 and 9 are written against the shape those test files
already use. Read the file before writing the test and adapt to its actual fixtures;
do not add parallel fixtures that duplicate existing ones.
