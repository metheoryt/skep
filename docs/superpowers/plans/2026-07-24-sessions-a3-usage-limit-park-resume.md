# Sessions A3 — usage-limit park & auto-resume — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recognise a Claude Code usage limit, park the session instead of failing it, and auto-resume it when the limit resets — with no human in the loop.

**Architecture:** The one unknown (the limit's wire shape) is isolated behind a single detector in `stream.py`. `Supervisor.run_events` turns a detected limit into a new `parked` terminal state carrying a `reset_at`, which rides the existing `done` event to the queen. The queen journal gains a `parked_until` column; a periodic **sweep** on the queen (mirroring the existing CEO-retry loop) resumes due-parked sessions through the same `cmd_resume` path a manual `/resume <ref>` uses. Resume itself reuses the already-built `Supervisor.resume` + `rebind_invocation`.

**Tech Stack:** Python 3.14, asyncio, aiohttp (queen WS + web app lifecycle), aiogram (Telegram), sqlite3 (`Bookkeeping`), pytest. Run tests with `uv run pytest`.

## Global Constraints

- **Python is invoked via `uv run` in this repo** — there is no bare `python` on PATH. Every test command is `uv run pytest …`.
- **The full suite is green at baseline: 411 passed, 3 skipped.** Never finish a task with fewer passing than before.
- **`reset_at` / `parked_until` are POSIX wall-clock timestamps (`float`), never monotonic.** They must survive a queen restart, so use `time.time`, injected as `now: Callable[[], float] = time.time` wherever read/written. (`QueenRouter` uses `time.monotonic` for `last_seen`; do **not** reuse that clock here.)
- **Wire frames carry ids/names only, never paths** (inherited from A2). The `resume` frame carries `session_local_id` + optional `model` string.
- **`parked` is a terminal state on the wire but a *live-but-idle* state in the journal.** On park, the session's worktree, topic, and mailbox persist; only the process is gone.
- **Only a positively-recognised limit parks.** An unrecognised error result still becomes `failed` — add no new way to mask a genuine failure as a park.
- **Follow existing patterns exactly:** the `done`/`on_done` threading mirrors how `session_local_id` was threaded in A2; the sweep mirrors `_install_ceo_retry` / `_ceo_retry_loop` in `queen/assembly.py`; the `/resume` command mirrors the `/kill` handler in `app.py`.

**Refinement of the spec:** the spec sketches `usage_limit_reset(ev) -> datetime | None`. This plan uses `detect_usage_limit(ev) -> UsageLimit | None` returning a dataclass whose `reset_at: float | None`, because the detector must distinguish three cases — *not a limit* (`None`), *a limit with a known reset* (`UsageLimit(reset_at=<ts>)`), and *a limit with an unknown reset* (`UsageLimit(reset_at=None)` → default backoff, §7). Timestamps are POSIX floats throughout for wire/DB consistency.

---

### Task 1: Bookkeeping learns the parked state

**Files:**
- Modify: `src/skep/queen/bookkeeping.py`
- Test: `tests/test_bookkeeping.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `Entry.parked_until: float | None` (new dataclass field, last).
  - `Bookkeeping.park(ref: int, until: float) -> None` — sets `status='parked', parked_until=until`.
  - `Bookkeeping.parked_due(now: float) -> list[Entry]` — `status='parked' AND parked_until <= now`, ordered by `ref`.
  - `Bookkeeping.rebind_invocation(ref, local_id)` now also clears `parked_until` (sets it NULL).
  - `list_active()` now includes `parked` rows (added to `_ACTIVE`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_bookkeeping.py`:

```python
def test_park_sets_status_and_until():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("h", "p", 1, "repo", "task", topic_id=10)
    bk.park(ref, until=1000.0)
    e = bk.get(ref)
    assert e.status == "parked"
    assert e.parked_until == 1000.0


def test_parked_due_returns_only_ripe_rows():
    bk = Bookkeeping.open(":memory:")
    a = bk.add("h", "p", 1, "r", "t", topic_id=1)
    b = bk.add("h", "p", 2, "r", "t", topic_id=2)
    bk.park(a, until=100.0)
    bk.park(b, until=300.0)
    due = bk.parked_due(now=200.0)
    assert [e.ref for e in due] == [a]


def test_rebind_invocation_clears_parked_until():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("h", "p", 1, "r", "t", topic_id=1)
    bk.park(ref, until=100.0)
    bk.rebind_invocation(ref, local_id=2)
    e = bk.get(ref)
    assert e.status == "running"
    assert e.parked_until is None
    assert e.local_id == 2


def test_list_active_includes_parked():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("h", "p", 1, "r", "t", topic_id=1)
    bk.park(ref, until=100.0)
    assert [e.ref for e in bk.list_active()] == [ref]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_bookkeeping.py -k "park or parked or clears or includes_parked" -v`
Expected: FAIL — `Entry.__init__() got an unexpected keyword argument 'parked_until'` / `AttributeError: 'Bookkeeping' object has no attribute 'park'`.

- [ ] **Step 3: Implement**

In `src/skep/queen/bookkeeping.py`:

Bump the schema version and extend the column tuple + dataclass:

```python
SCHEMA_VERSION = 2
```

```python
_ACTIVE = ("spawning", "running", "parked")
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
    "parked_until",
)
```

```python
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
    parked_until: float | None
```

Extend `_migrate` with the v1→v2 step (append after the existing `if version < 1:` block, before the final `PRAGMA user_version` is set — restructure so each version bump is independent):

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
    if version < 2:
        # v1 -> v2: a session can be parked (usage limit) and auto-resumed.
        # parked_until is a POSIX wall-clock ts; NULL for every non-parked row.
        conn.execute("ALTER TABLE entries ADD COLUMN parked_until REAL")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
```

Add the two methods and extend `rebind_invocation`:

```python
def park(self, ref: int, until: float) -> None:
    self._conn.execute(
        "UPDATE entries SET status='parked', parked_until=? WHERE ref=?",
        (until, ref),
    )
    self._conn.commit()

def parked_due(self, now: float) -> list[Entry]:
    rows = self._conn.execute(
        "SELECT * FROM entries WHERE status='parked' AND parked_until <= ?"
        " ORDER BY ref",
        (now,),
    ).fetchall()
    return [self._row(r) for r in rows]
```

```python
def rebind_invocation(self, ref: int, local_id: int) -> None:
    """Point an existing session's row at a new invocation.

    The ref, the topic and the session id all stay put -- that is what
    makes a topic follow a session across invocations. Resuming clears any
    park marker: the session is running again.
    """
    self._conn.execute(
        "UPDATE entries SET local_id=?, status='running', parked_until=NULL"
        " WHERE ref=?",
        (local_id, ref),
    )
    self._conn.commit()
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_bookkeeping.py -v`
Expected: PASS (all, including the pre-existing bookkeeping tests).

- [ ] **Step 5: Commit**

```bash
git add src/skep/queen/bookkeeping.py tests/test_bookkeeping.py
git commit -m "feat(queen): bookkeeping gains parked state + parked_until migration"
```

---

### Task 2: The usage-limit detector

**Files:**
- Modify: `src/skep/stream.py`
- Test: `tests/test_stream.py` (create if absent)

**Interfaces:**
- Consumes: `Event` (existing dataclass in `stream.py`, fields `kind`, `text`, `subtype`, `is_error`, `raw`).
- Produces:
  - `@dataclass class UsageLimit: reset_at: float | None`
  - `detect_usage_limit(ev: Event) -> UsageLimit | None` — `None` = not a limit; `UsageLimit(reset_at=ts)` = limit with a known POSIX reset; `UsageLimit(reset_at=None)` = recognised limit, reset unknown.

- [ ] **Step 1: Write the failing test**

Create `tests/test_stream.py` (or append if it exists):

```python
from skep.stream import Event, UsageLimit, detect_usage_limit


def _result(text: str, *, is_error: bool = True, raw: dict | None = None) -> Event:
    return Event(kind="result", text=text, is_error=is_error, raw=raw or {})


def test_non_error_result_is_not_a_limit():
    assert detect_usage_limit(_result("all done", is_error=False)) is None


def test_ordinary_error_is_not_a_limit():
    assert detect_usage_limit(_result("tool exploded")) is None


def test_limit_with_epoch_reset_in_raw():
    # The runner surfaces a machine-readable reset epoch when it has one.
    ev = _result(
        "Claude usage limit reached",
        raw={"subtype": "usage_limit", "reset_at": 1_700_000_000},
    )
    got = detect_usage_limit(ev)
    assert got == UsageLimit(reset_at=1_700_000_000.0)


def test_limit_without_reset_yields_unknown():
    ev = _result("Claude usage limit reached")
    got = detect_usage_limit(ev)
    assert got == UsageLimit(reset_at=None)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_stream.py -v`
Expected: FAIL — `ImportError: cannot import name 'UsageLimit'`.

- [ ] **Step 3: Implement**

In `src/skep/stream.py`, add near the top (after the `Event` dataclass):

```python
from dataclasses import dataclass


@dataclass
class UsageLimit:
    """A recognised usage limit. `reset_at` is a POSIX ts, or None if the
    runner gave no parseable reset (caller applies a default backoff)."""

    reset_at: float | None


# Best-guess text match. Hardened by a captured real fixture (design section 8.1);
# this is the ONLY place that changes when the real event shape lands.
_LIMIT_MARKERS = ("usage limit reached", "usage limit exceeded")


def detect_usage_limit(ev: Event) -> UsageLimit | None:
    if ev.kind != "result" or not ev.is_error:
        return None
    text = (ev.text or "").lower()
    raw = ev.raw or {}
    subtype = str(raw.get("subtype", "")).lower()
    is_limit = subtype == "usage_limit" or any(m in text for m in _LIMIT_MARKERS)
    if not is_limit:
        return None
    reset_raw = raw.get("reset_at")
    reset_at = float(reset_raw) if isinstance(reset_raw, (int, float)) else None
    return UsageLimit(reset_at=reset_at)
```

- [ ] **Step 4: Run the test**

Run: `uv run pytest tests/test_stream.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/skep/stream.py tests/test_stream.py
git commit -m "feat(stream): detect_usage_limit isolates the usage-limit shape"
```

---

### Task 3: Thread `reset_at` through the done event chain

Mechanical, no behaviour change yet: every `done`/`on_done`/wire hop gains an optional `reset_at: float | None = None`. This mirrors how `session_local_id` was threaded in A2.

**Files:**
- Modify: `src/skep/wire.py` (`done_msg`)
- Modify: `src/skep/transport.py` (`EventSink`, `QueenInbox`, `InMemoryEventSink`, `SwitchableEventSink`)
- Modify: `src/skep/ws_transport.py` (`WsEventSink.done` @289, `_dispatch` DONE branch @172-179)
- Modify: `src/skep/queen/telegram_sink.py` (`QueenSink.on_done` @72 — signature only in this task)
- Test: `tests/test_wire.py`, `tests/test_transport.py`

**Interfaces:**
- Produces: every sink hop accepts `reset_at: float | None = None`; `wire.done_msg(local_id, status, summary, reset_at=None)` puts `"reset_at"` on the frame; `_dispatch` reads `msg.get("reset_at")` and passes it to `on_done`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_wire.py`:

```python
def test_done_msg_carries_reset_at():
    from skep import wire

    msg = wire.done_msg(5, "parked", "limit", reset_at=1234.0)
    assert msg["reset_at"] == 1234.0


def test_done_msg_reset_at_defaults_none():
    from skep import wire

    assert wire.done_msg(5, "done", "ok")["reset_at"] is None
```

In `tests/test_transport.py` (append; a `QueenInbox`-recording stub likely exists — if not, add this minimal one):

```python
import asyncio


class _RecordingInbox:
    def __init__(self):
        self.done_calls = []

    async def on_task_started(self, *a, **k): ...
    async def on_activity(self, *a, **k): ...
    async def on_milestone(self, *a, **k): ...
    async def on_spawn_rejected(self, *a, **k): ...

    async def on_done(self, host, profile, local_id, status, summary, reset_at=None):
        self.done_calls.append((local_id, status, reset_at))


def test_inmemory_sink_forwards_reset_at():
    from skep.transport import InMemoryEventSink

    inbox = _RecordingInbox()
    sink = InMemoryEventSink(inbox, "h", "p")
    asyncio.run(sink.done(7, "parked", "limit", reset_at=999.0))
    assert inbox.done_calls == [(7, "parked", 999.0)]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_wire.py -k reset_at tests/test_transport.py -k forwards_reset_at -v`
Expected: FAIL — `done_msg() got an unexpected keyword argument 'reset_at'` / `on_done() ... unexpected keyword argument 'reset_at'`.

- [ ] **Step 3: Implement**

`src/skep/wire.py`:

```python
def done_msg(
    local_id: int, status: str, summary: str, reset_at: float | None = None
) -> dict[str, Any]:
    return {
        "t": DONE,
        "local_id": local_id,
        "status": status,
        "summary": summary,
        "reset_at": reset_at,
    }
```

`src/skep/transport.py` — update all four sites to append `reset_at: float | None = None`:

```python
# EventSink Protocol
async def done(
    self, local_id: int, status: str, summary: str, reset_at: float | None = None
) -> None: ...

# QueenInbox Protocol
async def on_done(
    self,
    host: str,
    profile: str,
    local_id: int,
    status: str,
    summary: str,
    reset_at: float | None = None,
) -> None: ...

# InMemoryEventSink
async def done(
    self, local_id: int, status: str, summary: str, reset_at: float | None = None
) -> None:
    await self._inbox.on_done(
        self._host, self._profile, local_id, status, summary, reset_at
    )

# SwitchableEventSink
async def done(
    self, local_id: int, status: str, summary: str, reset_at: float | None = None
) -> None:
    if self.target is not None:
        await self.target.done(local_id, status, summary, reset_at)
```

`src/skep/ws_transport.py` — `WsEventSink.done` (@289) and the `_dispatch` DONE branch (@172):

```python
# WsEventSink.done
async def done(
    self, local_id: int, status: str, summary: str, reset_at: float | None = None
) -> None:
    await self._send(wire.done_msg(local_id, status, summary, reset_at))
```

```python
# _dispatch, DONE branch
elif t == wire.DONE:
    await self._inbox.on_done(
        host,
        profile,
        int(msg["local_id"]),
        str(msg["status"]),
        str(msg["summary"]),
        msg.get("reset_at"),
    )
```

`src/skep/queen/telegram_sink.py` — extend the `on_done` signature only (body unchanged in this task):

```python
async def on_done(
    self,
    host: str,
    profile: str,
    local_id: int,
    status: str,
    summary: str,
    reset_at: float | None = None,
) -> None:
    entry = self._bk.by_worker_task(host, profile, local_id)
    if entry is None:
        return
    self._bk.set_status(entry.ref, status)
    if self._mailbox_service is not None:
        await self._mailbox_service.handle_recipient_gone(entry.ref)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_wire.py tests/test_transport.py tests/test_ws_transport.py -v`
Expected: PASS (new reset_at tests + all pre-existing transport tests still green).

- [ ] **Step 5: Commit**

```bash
git add src/skep/wire.py src/skep/transport.py src/skep/ws_transport.py src/skep/queen/telegram_sink.py tests/test_wire.py tests/test_transport.py
git commit -m "feat(transport): thread reset_at through the done event chain"
```

---

### Task 4: The queen parks on a parked done

Now `QueenSink.on_done` acts on `status == "parked"`: it parks the journal row (computing `parked_until` from `reset_at` or a default backoff, plus jitter), keeps the mailbox (no `handle_recipient_gone`), and posts the resume-time notice to the topic.

**Files:**
- Modify: `src/skep/queen/telegram_sink.py` (`QueenSink.__init__`, `on_done`)
- Modify: `src/skep/config.py` (`QueenConfig`, `load_queen_config`)
- Modify: `src/skep/queen/app.py` (`build_queen` — pass the new knobs into `QueenSink`)
- Test: `tests/test_telegram_sink.py` (exists — append)

**Interfaces:**
- Consumes: `Bookkeeping.park` (Task 1).
- Produces: `QueenSink(gateway, bookkeeping, mailbox_service=None, *, park_default_backoff: float = 3600.0, now: Callable[[], float] = time.time, jitter: Callable[[], float] = <0..60 uniform>)`. On `status == "parked"` it calls `bk.park(ref, until)` and posts one topic message; it does **not** call `handle_recipient_gone`.

- [ ] **Step 1: Write the failing test**

Create/append `tests/test_telegram_sink.py`:

```python
import asyncio
from types import SimpleNamespace

from skep.queen.bookkeeping import Bookkeeping
from skep.queen.telegram_sink import QueenSink


class _FakeGateway:
    def __init__(self):
        self.posts = []

    async def create_topic(self, name):
        return 100

    async def post(self, topic_id, text):
        self.posts.append((topic_id, text))
        return 1

    async def edit(self, *a, **k): ...


def _sink_with_entry():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("h", "p", 1, "repo", "task", topic_id=100)
    gw = _FakeGateway()
    sink = QueenSink(gw, bk, park_default_backoff=3600.0,
                     now=lambda: 1000.0, jitter=lambda: 0.0)
    return bk, ref, gw, sink


def test_parked_done_parks_with_known_reset():
    bk, ref, gw, sink = _sink_with_entry()
    asyncio.run(sink.on_done("h", "p", 1, "parked", "limit", reset_at=5000.0))
    e = bk.get(ref)
    assert e.status == "parked"
    assert e.parked_until == 5000.0
    assert gw.posts  # a "resumes ~..." notice was posted to the topic


def test_parked_done_uses_backoff_when_reset_unknown():
    bk, ref, gw, sink = _sink_with_entry()
    asyncio.run(sink.on_done("h", "p", 1, "parked", "limit", reset_at=None))
    e = bk.get(ref)
    assert e.parked_until == 1000.0 + 3600.0  # now + backoff, jitter=0


def test_parked_done_keeps_mailbox():
    bk, ref, gw, sink = _sink_with_entry()

    class _MB:
        def __init__(self): self.gone = []
        async def handle_recipient_gone(self, ref): self.gone.append(ref)

    mb = _MB()
    sink._mailbox_service = mb
    asyncio.run(sink.on_done("h", "p", 1, "parked", "limit", reset_at=5000.0))
    assert mb.gone == []  # parked session's mailbox is NOT torn down


def test_ordinary_done_still_sets_status_and_clears_mailbox():
    bk, ref, gw, sink = _sink_with_entry()

    class _MB:
        def __init__(self): self.gone = []
        async def handle_recipient_gone(self, ref): self.gone.append(ref)

    mb = _MB()
    sink._mailbox_service = mb
    asyncio.run(sink.on_done("h", "p", 1, "done", "ok", reset_at=None))
    assert bk.get(ref).status == "done"
    assert mb.gone == [ref]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_telegram_sink.py -v`
Expected: FAIL — `QueenSink.__init__() got an unexpected keyword argument 'park_default_backoff'`.

- [ ] **Step 3: Implement**

`src/skep/queen/telegram_sink.py` — imports and constructor:

```python
import random
import time
from collections.abc import Callable
from datetime import datetime
```

```python
def __init__(
    self,
    gateway: Gateway,
    bookkeeping: Bookkeeping,
    mailbox_service: MailboxService | None = None,
    *,
    park_default_backoff: float = 3600.0,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] = lambda: random.uniform(0.0, 60.0),
) -> None:
    self._gw = gateway
    self._bk = bookkeeping
    self._mailbox_service = mailbox_service
    self._park_default_backoff = park_default_backoff
    self._now = now
    self._jitter = jitter
```

Replace `on_done`:

```python
async def on_done(
    self,
    host: str,
    profile: str,
    local_id: int,
    status: str,
    summary: str,
    reset_at: float | None = None,
) -> None:
    entry = self._bk.by_worker_task(host, profile, local_id)
    if entry is None:
        return
    if status == "parked":
        base = reset_at if reset_at is not None else self._now() + self._park_default_backoff
        until = base + self._jitter()
        self._bk.park(entry.ref, until)
        when = datetime.fromtimestamp(until).strftime("%H:%M")
        await self._gw.post(
            entry.topic_id, escape_md(f"⏸ parked (usage limit) · resumes ~{when}")
        )
        return
    self._bk.set_status(entry.ref, status)
    if self._mailbox_service is not None:
        await self._mailbox_service.handle_recipient_gone(entry.ref)
```

`src/skep/config.py` — add to `QueenConfig` (after `mailbox_ceo_retry_interval`):

```python
    # Usage-limit park/resume knobs
    park_sweep_interval: float = 30.0
    park_default_backoff: float = 3600.0
```

In `load_queen_config`, add to the `QueenConfig(...)` call:

```python
        park_sweep_interval=float(env.get("SKEP_PARK_SWEEP_INTERVAL", "30")),
        park_default_backoff=float(env.get("SKEP_PARK_DEFAULT_BACKOFF", "3600")),
```

`src/skep/queen/app.py` — in `build_queen`, pass the backoff to the sink:

```python
    sink = QueenSink(
        gateway,
        bk,
        mailbox_service=mailbox_service,
        park_default_backoff=qcfg.park_default_backoff,
    )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_telegram_sink.py tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/skep/queen/telegram_sink.py src/skep/config.py src/skep/queen/app.py tests/test_telegram_sink.py
git commit -m "feat(queen): park the journal on a parked done, keep the mailbox"
```

---

### Task 5: The worker parks on a detected usage limit

`Supervisor.run_events` turns a detected limit into `terminal="parked"` and forwards `reset_at` on the `done` event. The resume_token is already persisted (existing lines 316/322), so the session stays resumable.

**Files:**
- Modify: `src/skep/supervisor.py` (`run_events` @308-361)
- Test: `tests/test_supervisor.py` (find the module that already tests `run_events`; check `tests/` for `run_events` usages first)

**Interfaces:**
- Consumes: `detect_usage_limit` (Task 2), `EventSink.done(..., reset_at=)` (Task 3).
- Produces: on a usage-limit `result`, `run_events` sets `terminal="parked"` and calls `self._sink.done(task_id, "parked", summary, reset_at=<float|None>)`.

- [ ] **Step 1: Write the failing test**

Find how the suite already exercises `run_events` (a fake agent yielding `Event`s and a recording sink). Model the new test on it. Skeleton:

```python
def test_run_events_parks_on_usage_limit(monkeypatch):
    # ... build a Supervisor with a recording sink and a fake agent whose
    # events() yields:
    #   Event(kind="system", session_id="tok-1")
    #   Event(kind="result", text="Claude usage limit reached",
    #         is_error=True, raw={"subtype": "usage_limit", "reset_at": 5000})
    # then run: asyncio.run(sup.run_events(tid, agent))
    # assert the recorded done call == (tid, "parked", <summary>, 5000.0)
    # assert the registry row's status == "parked"
    # assert the resume_token was persisted == "tok-1"
    ...
```

Write it concretely against the existing test fixtures in that module (reuse its fake-agent + recording-sink helpers rather than inventing new ones).

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_supervisor.py -k parks_on_usage_limit -v`
Expected: FAIL — the recorded `done` status is `"failed"`, not `"parked"`.

- [ ] **Step 3: Implement**

In `src/skep/supervisor.py`, import the detector at the top:

```python
from skep.stream import detect_usage_limit
```

In `run_events`, add a park-reset local and branch on the `result`:

```python
    async def run_events(self, task_id: int, agent: AgentProcess) -> None:
        activity_started = False
        terminal = "done"
        saw_result = False
        summary = ""
        park_reset: float | None = None
        try:
            async for ev in agent.events():
                if ev.kind == "system" and ev.session_id:
                    self._reg.update(task_id, resume_token=ev.session_id)
                if ev.kind == "result":
                    saw_result = True
                    summary = ev.text
                    self._reg.update(
                        task_id,
                        resume_token=ev.session_id or self._task(task_id).resume_token,
                    )
                    limit = detect_usage_limit(ev)
                    if limit is not None:
                        terminal = "parked"
                        park_reset = limit.reset_at
                    else:
                        terminal = "failed" if ev.is_error else "done"
                # ... activity_line / milestone handling unchanged ...
```

In the `finally` block, keep the `killed` override and pass `reset_at` to the sink:

```python
        finally:
            if self._task(task_id).status == "killed":
                terminal = "killed"
                park_reset = None
            self._reg.update(task_id, status=terminal)
            self._agents.pop(task_id, None)
            shim = self._shims.pop(task_id, None)
            if shim is not None:
                try:
                    await shim.stop()
                except Exception as exc:
                    self._reg.log_audit(
                        task_id, "error", f"mailbox shim stop failed: {exc}"
                    )
            _ = activity_started
            await self._sink.done(task_id, terminal, summary, reset_at=park_reset)
```

(Leave the `if not saw_result ...` and `except Exception` branches as they are — a crash or no-result is a `failed`, never a park.)

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_supervisor.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/skep/supervisor.py tests/test_supervisor.py
git commit -m "feat(worker): run_events parks the session on a usage limit"
```

---

### Task 6: The `resume` command crosses the wire

Add the `resume` frame and the worker's dispatch to `Supervisor.resume` (which already exists).

**Files:**
- Modify: `src/skep/wire.py` (`RESUME` constant, `resume_msg`)
- Modify: `src/skep/transport.py` (`CommandHandler` Protocol)
- Modify: `src/skep/ws_transport.py` (`WsCommandClient.resume` near @45, `_on_command` RESUME branch near @412)
- Test: `tests/test_wire.py`, `tests/test_ws_transport.py`

**Interfaces:**
- Consumes: `Supervisor.resume(session_local_id: int, *, model: str | None = None) -> int` (exists, supervisor.py:252).
- Produces:
  - `wire.resume_msg(session_local_id: int, model: str | None = None) -> dict` → `{"t":"resume","session_local_id":..,"model":..}`.
  - `CommandHandler.resume(session_local_id: int, model: str | None = None) -> int`.
  - Worker `_on_command` handles `wire.RESUME` → `await self._sup.resume(int(msg["session_local_id"]), model=msg.get("model"))`, catching `CapacityError`/`ValueError` and reporting via `spawn_rejected_msg` (reuses the existing rejection channel).

- [ ] **Step 1: Write the failing tests**

`tests/test_wire.py`:

```python
def test_resume_msg_shape():
    from skep import wire

    m = wire.resume_msg(42, model="opus")
    assert m == {"t": "resume", "session_local_id": 42, "model": "opus"}


def test_resume_msg_model_optional():
    from skep import wire

    assert wire.resume_msg(42)["model"] is None
```

`tests/test_ws_transport.py` — mirror the existing spawn/kill round-trip test (there is one that drives a `CommandHandler` call to the worker's `Supervisor` stub). Add:

```python
def test_resume_frame_reaches_supervisor():
    # Using the same harness the spawn/kill round-trip test uses:
    # a WsCommandClient on the queen side, a fake Supervisor recording resume().
    # Drive: await client.resume(7, model="opus")
    # Assert the fake Supervisor recorded resume(session_local_id=7, model="opus").
    ...
```

Write it concretely against that module's existing fixtures.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_wire.py -k resume tests/test_ws_transport.py -k resume_frame -v`
Expected: FAIL — `module 'skep.wire' has no attribute 'resume_msg'`.

- [ ] **Step 3: Implement**

`src/skep/wire.py`:

```python
RESUME = "resume"
```

```python
def resume_msg(session_local_id: int, model: str | None = None) -> dict[str, Any]:
    return {"t": RESUME, "session_local_id": session_local_id, "model": model}
```

`src/skep/transport.py` — add to the `CommandHandler` Protocol:

```python
    async def resume(
        self, session_local_id: int, model: str | None = None
    ) -> int: ...
```

`src/skep/ws_transport.py` — `WsCommandClient` (next to `spawn`/`kill`):

```python
    async def resume(
        self, session_local_id: int, model: str | None = None
    ) -> int:
        await self._ws.send_str(
            wire.encode(wire.resume_msg(session_local_id, model))
        )
        return 0
```

`_on_command` — add a branch after `KILL`/`PANIC`:

```python
        elif t == wire.RESUME:
            try:
                await self._sup.resume(
                    int(msg["session_local_id"]), model=msg.get("model")
                )
            except (CapacityError, ValueError) as exc:
                await ws.send_str(wire.encode(wire.spawn_rejected_msg(str(exc))))
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_wire.py tests/test_ws_transport.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/skep/wire.py src/skep/transport.py src/skep/ws_transport.py tests/test_wire.py tests/test_ws_transport.py
git commit -m "feat(transport): resume command crosses the wire to Supervisor.resume"
```

---

### Task 7: `QueenRouter.cmd_resume` and the concurrency guard

**Files:**
- Modify: `src/skep/queen/router.py` (`cmd_resume`)
- Test: `tests/test_router.py` (the module that tests `cmd_spawn`/`cmd_kill`; locate it first)

**Interfaces:**
- Consumes: `Bookkeeping.get`, `CommandHandler.resume`.
- Produces: `QueenRouter.cmd_resume(ref: int, model: str | None = None) -> bool` — returns `False` (no state change) when the ref is unknown, its worker is offline, or the entry is already `running`; otherwise routes `handler.resume(session_local_id, model)` and returns `True`.

- [ ] **Step 1: Write the failing tests**

In the router test module (mirror how `cmd_kill` is tested — a `Bookkeeping` + a recording fake handler registered under `(host, profile)`):

```python
def test_cmd_resume_routes_to_worker():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("h", "p", 1, "r", "t", topic_id=1)
    bk.park(ref, until=100.0)
    router = QueenRouter(bk)
    handler = _RecordingHandler()  # records resume(session_local_id, model)
    router.register("h", "p", handler)
    router.mark_online("h", "p")
    ok = asyncio.run(router.cmd_resume(ref))
    assert ok is True
    assert handler.resumed == [(1, None)]  # session_local_id == local_id here


def test_cmd_resume_unknown_ref_is_false():
    router = QueenRouter(Bookkeeping.open(":memory:"))
    assert asyncio.run(router.cmd_resume(999)) is False


def test_cmd_resume_skips_running_entry():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("h", "p", 1, "r", "t", topic_id=1)  # status defaults to running
    router = QueenRouter(bk)
    handler = _RecordingHandler()
    router.register("h", "p", handler)
    ok = asyncio.run(router.cmd_resume(ref))
    assert ok is False
    assert handler.resumed == []


def test_cmd_resume_offline_worker_is_false():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("h", "p", 1, "r", "t", topic_id=1)
    bk.park(ref, until=100.0)
    router = QueenRouter(bk)  # no handler registered
    assert asyncio.run(router.cmd_resume(ref)) is False
```

If the router test module lacks a `_RecordingHandler`, add one implementing `spawn`/`kill`/`panic`/`resume`, recording `resume` args.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_router.py -k cmd_resume -v`
Expected: FAIL — `'QueenRouter' object has no attribute 'cmd_resume'`.

- [ ] **Step 3: Implement**

In `src/skep/queen/router.py`, after `cmd_kill`:

```python
    async def cmd_resume(self, ref: int, model: str | None = None) -> bool:
        entry = self._bk.get(ref)
        if entry is None:
            return False
        # Guard: only a non-running session may resume. The first caller to
        # rebind flips status to 'running' (Bookkeeping.rebind_invocation), so a
        # racing manual /resume and the auto-sweep cannot double-invoke.
        if entry.status == "running":
            return False
        handler = self._workers.get((entry.host, entry.profile))
        if handler is None:
            return False
        await handler.resume(entry.session_local_id, model)
        return True
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_router.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/skep/queen/router.py tests/test_router.py
git commit -m "feat(queen): cmd_resume routes to the worker, guards on running"
```

---

### Task 8: The `/resume` Telegram command

**Files:**
- Modify: `src/skep/app.py` (add a `/resume` handler next to `/kill` @190-196)
- Test: `tests/test_app.py` (the module that tests the `/kill` / `/spawn` handlers)

**Interfaces:**
- Consumes: `QueenRouter.cmd_resume` (Task 7).
- Produces: `/resume <ref> [--model <m>]` → parses the ref (and optional `--model`), calls `router.cmd_resume(ref, model)`, answers `"Resuming ref <n>"` on `True` else `"No such session / already running"`.

- [ ] **Step 1: Write the failing test**

Mirror the existing `/kill` handler test. Model:

```python
def test_resume_command_calls_cmd_resume():
    # Build the dispatcher with a recording router whose cmd_resume returns True.
    # Feed a Message with text "/resume 5".
    # Assert router.cmd_resume was awaited with (5, None) and the bot answered
    # a "Resuming" message.
    ...


def test_resume_command_rejects_non_numeric():
    # "/resume abc" -> answers the usage string, cmd_resume never called.
    ...
```

Write concretely against `test_app.py`'s existing dispatcher-driving harness.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_app.py -k resume -v`
Expected: FAIL — no `/resume` handler registered (the recording router's `cmd_resume` is never called).

- [ ] **Step 3: Implement**

In `src/skep/app.py`, after the `_kill` handler:

```python
    @dp.message(Command("resume"), F.func(owner_only))
    async def _resume(message: Message, command: CommandObject) -> None:
        args = (command.args or "").split()
        model: str | None = None
        if "--model" in args:
            i = args.index("--model")
            model = args[i + 1] if i + 1 < len(args) else None
            args = args[:i] + args[i + 2 :]
        if len(args) != 1 or not args[0].isdigit():
            await message.answer(
                "Usage: /resume <ref> [--model <m>]", parse_mode=None
            )
            return
        ok = await router.cmd_resume(int(args[0]), model)
        await message.answer(
            f"Resuming ref {args[0]}" if ok else "No such session / already running",
            parse_mode=None,
        )
```

- [ ] **Step 4: Run the test**

Run: `uv run pytest tests/test_app.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/skep/app.py tests/test_app.py
git commit -m "feat(queen): /resume <ref> command drives cmd_resume"
```

---

### Task 9: The auto-resume sweep

A periodic loop on the queen resumes due-parked sessions, mirroring `_install_ceo_retry` / `_ceo_retry_loop`.

**Files:**
- Modify: `src/skep/queen/assembly.py` (`_park_sweep_loop`, `_install_park_sweep`)
- Modify: `src/skep/queen/app.py` (call `_install_park_sweep` in `build_queen`)
- Test: `tests/queen/test_park_sweep.py` (or wherever `_ceo_retry_loop` is tested)

**Interfaces:**
- Consumes: `Bookkeeping.parked_due` (Task 1), `QueenRouter.cmd_resume` + `QueenRouter.is_online` (Task 7), `CapacityError`.
- Produces: `_park_sweep_loop(bk, router, interval, now=time.time)` — every `interval`s, for each `bk.parked_due(now())` on an online worker, `await router.cmd_resume(entry.ref)`, swallowing `CapacityError` and any per-entry exception so one bad entry never kills the loop. `_install_park_sweep(app, bk, router, interval)` ties it to the app lifecycle via `app.cleanup_ctx`.

- [ ] **Step 1: Write the failing test**

`tests/queen/test_park_sweep.py`:

```python
import asyncio

from skep.queen.assembly import _park_sweep_loop
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.router import QueenRouter


class _Handler:
    def __init__(self): self.resumed = []
    async def spawn(self, *a, **k): return 0
    async def kill(self, *a, **k): return True
    async def panic(self): return 0
    async def resume(self, sid, model=None): self.resumed.append(sid)


def test_sweep_resumes_due_parked_on_online_worker():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("h", "p", 1, "r", "t", topic_id=1)
    bk.park(ref, until=100.0)
    router = QueenRouter(bk)
    h = _Handler()
    router.register("h", "p", h)
    router.mark_online("h", "p")

    async def drive():
        # one tick: a huge interval so the loop sleeps after a single pass,
        # then cancel it.
        task = asyncio.create_task(
            _park_sweep_loop(bk, router, interval=3600.0, now=lambda: 200.0)
        )
        await asyncio.sleep(0)          # let the first pass run
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert h.resumed == [1]


def test_sweep_skips_offline_worker():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("h", "p", 1, "r", "t", topic_id=1)
    bk.park(ref, until=100.0)
    router = QueenRouter(bk)
    h = _Handler()
    router.register("h", "p", h)   # registered but NOT marked online

    async def drive():
        task = asyncio.create_task(
            _park_sweep_loop(bk, router, interval=3600.0, now=lambda: 200.0)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert h.resumed == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/queen/test_park_sweep.py -k sweep -v`
Expected: FAIL — `cannot import name '_park_sweep_loop'`.

- [ ] **Step 3: Implement**

In `src/skep/queen/assembly.py` (near `_ceo_retry_loop` / `_install_ceo_retry`), add:

```python
import time
from collections.abc import Callable

from skep.queen.router import QueenRouter
from skep.supervisor import CapacityError


async def _park_sweep_loop(
    bk: Bookkeeping,
    router: QueenRouter,
    interval: float,
    now: Callable[[], float] = time.time,
) -> None:
    """Periodically auto-resume due-parked sessions.

    Edges fall out of re-evaluation, not bespoke handling: an offline worker is
    skipped and retried next tick; a full worker raises CapacityError and is
    retried next tick; a queen restart just resumes finding due rows (parked_until
    lives in the journal). Never lets one bad entry kill the loop.
    """
    while True:
        try:
            for entry in bk.parked_due(now()):
                if not router.is_online(entry.host, entry.profile):
                    continue
                try:
                    await router.cmd_resume(entry.ref)
                except CapacityError:
                    continue
                except Exception:
                    log.warning("park sweep: resume of ref %s failed",
                                entry.ref, exc_info=True)
        except Exception:
            log.warning("park sweep pass failed", exc_info=True)
        await asyncio.sleep(interval)


def _install_park_sweep(
    app: web.Application,
    bk: Bookkeeping,
    router: QueenRouter,
    interval: float,
) -> None:
    """Tie the park sweep to the web app's lifecycle (mirrors _install_ceo_retry)."""

    async def _ctx(app: web.Application) -> AsyncIterator[None]:
        task = asyncio.create_task(_park_sweep_loop(bk, router, interval))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app.cleanup_ctx.append(_ctx)
```

(Reuse the module's existing `log`, `asyncio`, `contextlib`, `web`, `AsyncIterator`, `Bookkeeping` imports; add only `time`, `Callable`, `QueenRouter`, `CapacityError` if missing. Guard against an import cycle — if importing `QueenRouter` at module top cycles, import it inside the function signature via `TYPE_CHECKING` and annotate as a string.)

`src/skep/queen/app.py` — in `build_queen`, after the CEO-retry install:

```python
    if qcfg.park_sweep_interval > 0:
        _install_park_sweep(app, bk, router, qcfg.park_sweep_interval)
```

and extend the import from `skep.queen.assembly` to include `_install_park_sweep`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/queen/test_park_sweep.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/skep/queen/assembly.py src/skep/queen/app.py tests/queen/test_park_sweep.py
git commit -m "feat(queen): auto-resume sweep resumes due-parked sessions"
```

---

### Task 10: End-to-end park→auto-resume, and documentation

Prove the whole path with one integration test, then update `ARCHITECTURE.md` and project memory.

**Files:**
- Test: `tests/test_integration.py` (append)
- Modify: `ARCHITECTURE.md`
- Modify: `.claude/memory/project.md`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the failing end-to-end test**

Append to `tests/test_integration.py`, reusing its existing queen+worker harness (the one that already drives a spawn end-to-end at ~line 133). The test:

1. Spawns a session; drives the worker agent to emit a usage-limit `result`
   (`raw={"subtype":"usage_limit","reset_at":<past ts>}`).
2. Asserts the queen journal row is `status=="parked"` with the expected
   `parked_until`.
3. Runs one park-sweep pass with `now` past `parked_until` and the worker online.
4. Asserts the worker received a `resume` and the journal row is back to
   `status=="running"` on the **same** `ref` and topic (via `rebind_invocation`).

```python
def test_usage_limit_parks_then_sweep_resumes():
    # ... build the integration harness, then the four steps above ...
    ...
```

Write it concretely against the module's fixtures (fake claude / fake gateway / in-memory transport).

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_integration.py -k parks_then_sweep -v`
Expected: FAIL (initially because the harness needs wiring; iterate until it fails for the *right* reason — the assertion, not a setup error).

- [ ] **Step 3: Make it pass**

No new production code should be needed — the path is built in Tasks 1-9. Fix only test wiring until green.

Run: `uv run pytest tests/test_integration.py -v`
Expected: PASS.

- [ ] **Step 4: Update the docs**

`ARCHITECTURE.md` — in the inbound/outbound message-path section, add `parked` to the terminal states and describe the park→sweep→resume loop and the `resume` frame. Mark it **live**. Refresh the stamp line at the top to the new HEAD once committed.

`.claude/memory/project.md` — add one bullet under `## Decisions`:

```markdown
- **Sessions A3 DONE (2026-07-24, branch `metheoryt/ubuntu26-skep-A2-resume`).**
  Usage-limit park + auto-resume. `detect_usage_limit` (stream.py) isolates the
  limit shape; `run_events` emits a `parked` terminal carrying `reset_at`;
  `Bookkeeping.parked_until` (schema v2) + `park`/`parked_due`; `QueenSink`
  parks (keeps the mailbox, posts "resumes ~HH:MM", default backoff 1h + 0-60s
  jitter when reset unknown); `/resume <ref> [--model]` + `resume` wire frame →
  `Supervisor.resume` (A1); a queen `_park_sweep_loop` (mirrors CEO-retry) auto-
  resumes due-parked sessions on online workers. **Deferred:** P2 multi-account
  pool (gated on the credential-injection spike — the Orca-spawned claude carries
  no credential env var, mechanism unconfirmed/possibly racy) and P3 per-subagent
  model. **Residual:** detection is a text heuristic until a real usage-limit
  event is captured (design section 8.1).
```

- [ ] **Step 5: Commit**

```bash
git add tests/test_integration.py ARCHITECTURE.md .claude/memory/project.md
git commit -m "test(sessions): e2e park->sweep->resume; refresh architecture and memory"
```

---

## Self-Review Notes

- **Spec §3 (detector + parked terminal):** Tasks 2 (detector) + 5 (run_events parks). ✓
- **Spec §3 (`done` gains `reset_at`):** Task 3 threads it through every hop. ✓
- **Spec §4 (journal parked_until + migration + parked_due + format_ls):** Task 1. `format_ls` shows `parked` for free via `_ACTIVE`. ✓
- **Spec §5 (resume drive path mirrors /spawn):** Tasks 6 (wire+worker), 7 (cmd_resume+guard), 8 (/resume). `Supervisor.resume` + `rebind_invocation` pre-exist. ✓
- **Spec §6 (sweep + edge table):** Task 9. Offline/capacity/restart/double-fire all covered by re-evaluation + the Task 7 guard. ✓
- **Spec §7 (no-reset default backoff + jitter):** Task 4 (`park_default_backoff` + `jitter`). ✓
- **Spec §8.1 (usage-limit capture):** carried as a residual in Task 2's comment + Task 10's memory bullet — the detector is the single change point. §8.2 (credential spike) is P2, out of scope. ✓
- **Type consistency:** `reset_at: float | None` and `parked_until: float | None` are floats end-to-end; `detect_usage_limit -> UsageLimit | None`; `cmd_resume(ref, model=None) -> bool`; `resume(session_local_id, model=None)`. Names match across tasks. ✓
- **Naming note for the implementer:** the spec's `usage_limit_reset` is realised as `detect_usage_limit` returning a `UsageLimit` dataclass (see Global Constraints refinement) — deliberate, to separate "not a limit" from "limit, reset unknown".
```
