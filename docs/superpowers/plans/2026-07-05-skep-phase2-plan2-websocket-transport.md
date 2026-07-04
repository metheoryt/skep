# skep Phase 2 — Plan 2: WebSocket transport + split entrypoints

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Plan 1's in-memory queen↔worker link with a real **WebSocket transport** — mutually authenticated (HMAC challenge-response), with mDNS discovery, heartbeat/presence, reconnect/re-attach, and two split entrypoints (`skep-queen`, `skepd`) — so a queen on one host can drive workers on other hosts over LAN, WireGuard, or `wss://skep.cyphy.kz`.

**Architecture:** The Plan-1 seam interfaces (`EventSink`, `CommandHandler`, `QueenInbox`) are unchanged; Plan 2 adds new *concrete* implementations that carry the same calls over a socket. Worker side: a `WorkerWsClient` connects out to the queen, runs the client half of the auth handshake, `register`s its `(host, profile)` + active tasks, exposes a `WsEventSink` (an `EventSink` that serialises each domain event to a JSON frame), and dispatches inbound command frames to its local `Supervisor` (which already *is* a `CommandHandler`). Queen side: a `QueenWsServer` (aiohttp) accepts a connection, runs the server half of the handshake, reads `register`, wraps the socket in a `RemoteWorker` (a `CommandHandler` that sends command frames) and registers it in the existing `QueenRouter` under `(host, profile)`, then pumps inbound event frames into the existing `QueenSink` (`QueenInbox`). The Supervisor holds a `SwitchableEventSink` whose target is swapped per connection, so a dropped link never kills running agents — reporting just pauses until reconnect.

**Tech Stack:** Python 3.13, asyncio, aiohttp (WS server + client), `aiogram` 3.x (Telegram long-polling, queen only), `zeroconf` (mDNS), stdlib `hmac`/`secrets`/`json`/`sqlite3`, `uv`, `pytest` + `pytest-asyncio`.

## Global Constraints

- Python **3.13**; asyncio throughout; `from __future__ import annotations` at the top of every new module.
- **`src` must stay pyright-clean** (`uvx pyright src` → 0 errors). Mirror the existing idioms: the `_task()` assert-style helper for narrowing `Task | None`, and `Callable[..., X]` annotations for injected factories. Every task ends by confirming pyright is clean.
- **Seam interfaces are frozen except for one documented extension** — `QueenInbox` gains `on_spawn_rejected` (Task 4). `EventSink` and `CommandHandler` signatures do **not** change; Plan 2 only adds new implementers.
- **`host` and `profile` are separate fields everywhere** — never concatenated into a parseable id. On the wire they are two JSON fields; by-id commands use the queen's opaque global `ref`.
- **All outbound Telegram text is MarkdownV2, escaped on the queen only.** Workers send plain semantic text over the wire; `QueenSink` calls `escape_md` before sending. Unchanged from Plan 1.
- **Auth (Telegram, unchanged):** every Telegram update rejected unless `from_user.id == owner_id`, via `dp.update.outer_middleware` + per-handler `F.func(owner_only)`.
- **Transport auth is mandatory and mutual:** every worker↔queen connection completes an HMAC challenge-response over fresh nonces (both sides prove the shared secret) *before* any `register`/command/event frame is accepted. A `spawn` frame is remote code execution on the worker — an unauthenticated or wrong-secret peer must be rejected by **both** sides.
- **Correctness invariant (design §6.4):** *worker offline ≠ its agents dead.* On a dropped link the queen marks the **worker** detached but never marks its tasks failed/killed; the worker's `Supervisor` and its `claude` subprocesses keep running, only reporting pauses.
- **Do NOT physically move `telegram_gw.py` / `formatting.py`** into `queen/`. They are logically the queen's and are already imported only by the queen package; moving them risks re-introducing the type-annotation coupling regression noted in project memory. Leave them at `src/skep/`.
- **Agent runtime unchanged:** `claude -p "<task>" --output-format stream-json --verbose`, `stdin=DEVNULL`. `--input-format`/stdin stay deferred to Phase 3.
- Deployment wiring (Caddyfile vhost, `homeserver/skep-queen/compose.yml`) lives in `~/gh/vps` and is **out of scope** for this repo. This plan only fixes the contract: queen listens on `SKEP_LISTEN_HOST:SKEP_LISTEN_PORT` (default `0.0.0.0:8765`), path `/ws`, reads `SKEP_SHARED_SECRET`, advertises `SKEP_PUBLIC_URL`.

## File Structure

```
src/skep/
  config.py            # MODIFY: QueenConfig += listen_host/port, shared_secret, public_url, advertise_mdns
                        #         WorkerConfig += queen_url, shared_secret, use_mdns
  wire.py              # CREATE: message-type tags + encode/decode + typed builders
  auth.py              # CREATE: HMAC challenge-response handshake (server + client halves)
  transport.py         # MODIFY: QueenInbox += on_spawn_rejected; add SwitchableEventSink
  ws_transport.py      # CREATE: WsEventSink, RemoteWorker, QueenWsServer, WorkerWsClient
  discovery.py         # CREATE: mDNS advertise (queen) + browse (worker); --queen-url fallback
  supervisor.py        # (unchanged — already a CommandHandler + EventSink emitter)
  queen/
    telegram_sink.py   # MODIFY: implement on_spawn_rejected; make on_task_started re-attach-idempotent (Task 7)
    router.py          # MODIFY: unregister(); presence (online/last_seen); detached marker in format_ls
    app.py             # CREATE: skep-queen entrypoint (bot + dispatcher + WS server + mDNS advertise)
  worker/
    __init__.py        # CREATE
    app.py             # CREATE: skepd entrypoint (Supervisor + WS client reconnect loop + mDNS browse)
  app.py               # (unchanged — Plan-1 in-memory single-process launcher, kept for the in-memory integration test + local co-located dev)
pyproject.toml         # MODIFY: + aiohttp (Task 4), + zeroconf (Task 8); console scripts skep-queen (Task 9), skepd (Task 10)
tests/
  test_config.py       # MODIFY
  test_wire.py         # CREATE
  test_auth.py         # CREATE
  test_transport.py    # MODIFY (SwitchableEventSink)
  test_ws_transport.py # CREATE
  test_router.py       # MODIFY (presence + detached)
  test_discovery.py    # CREATE
  test_queen_app.py    # CREATE
  test_worker_app.py   # CREATE
  test_onboarding.py   # CREATE
  test_integration.py  # MODIFY (two-worker WS e2e + auth-reject e2e)
```

---

## Task 1: Config — network + auth fields

**Files:**
- Modify: `src/skep/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `QueenConfig` gains: `listen_host: str = "0.0.0.0"`, `listen_port: int = 8765`, `shared_secret: str = ""`, `public_url: str | None = None`, `advertise_mdns: bool = True`.
  - `WorkerConfig` gains: `queen_url: str | None = None`, `shared_secret: str = ""`, `use_mdns: bool = True`.
  - Env keys: `SKEP_LISTEN_HOST`, `SKEP_LISTEN_PORT`, `SKEP_SHARED_SECRET`, `SKEP_PUBLIC_URL`, `SKEP_ADVERTISE_MDNS`, `SKEP_QUEEN_URL`, `SKEP_USE_MDNS`. Booleans parse `"1"/"true"/"yes"` (case-insensitive) as true.

- [ ] **Step 1: Add failing tests to `tests/test_config.py`**

Append these tests (keep the existing ones):

```python
def test_queen_config_network_defaults():
    cfg = load_queen_config(_queen_env())
    assert cfg.listen_host == "0.0.0.0"
    assert cfg.listen_port == 8765
    assert cfg.shared_secret == ""
    assert cfg.public_url is None
    assert cfg.advertise_mdns is True


def test_queen_config_network_overrides():
    env = _queen_env() | {
        "SKEP_LISTEN_HOST": "10.0.0.2",
        "SKEP_LISTEN_PORT": "9000",
        "SKEP_SHARED_SECRET": "s3cr3t",
        "SKEP_PUBLIC_URL": "wss://skep.cyphy.kz/ws",
        "SKEP_ADVERTISE_MDNS": "false",
    }
    cfg = load_queen_config(env)
    assert cfg.listen_host == "10.0.0.2"
    assert cfg.listen_port == 9000
    assert cfg.shared_secret == "s3cr3t"
    assert cfg.public_url == "wss://skep.cyphy.kz/ws"
    assert cfg.advertise_mdns is False


def test_worker_config_transport_defaults():
    cfg = load_worker_config(_worker_env())
    assert cfg.queen_url is None
    assert cfg.shared_secret == ""
    assert cfg.use_mdns is True


def test_worker_config_transport_overrides():
    env = _worker_env() | {
        "SKEP_QUEEN_URL": "wss://skep.cyphy.kz/ws",
        "SKEP_SHARED_SECRET": "s3cr3t",
        "SKEP_USE_MDNS": "0",
    }
    cfg = load_worker_config(env)
    assert cfg.queen_url == "wss://skep.cyphy.kz/ws"
    assert cfg.shared_secret == "s3cr3t"
    assert cfg.use_mdns is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL (`TypeError: __init__() got an unexpected keyword argument` / `AttributeError`).

- [ ] **Step 3: Implement the config changes**

In `src/skep/config.py`, add a boolean parser and extend both dataclasses + loaders:

```python
def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")
```

Extend `QueenConfig` (add fields after `group_chat_id`, before `bookkeeping_db`):

```python
@dataclass(frozen=True)
class QueenConfig:
    bot_token: str
    owner_id: int
    group_chat_id: int
    listen_host: str = "0.0.0.0"
    listen_port: int = 8765
    shared_secret: str = ""
    public_url: str | None = None
    advertise_mdns: bool = True
    bookkeeping_db: str = "queen.sqlite"
```

Extend `WorkerConfig` (add fields after `max_concurrent`, keep `claude_bin` last):

```python
@dataclass(frozen=True)
class WorkerConfig:
    host: str
    profile: str
    claude_config_dir: str | None
    repos_root: Path
    worktrees_root: Path
    db_path: str
    max_concurrent: int = 8
    queen_url: str | None = None
    shared_secret: str = ""
    use_mdns: bool = True
    claude_bin: str = "claude"
```

Update `load_queen_config` to read the new keys:

```python
def load_queen_config(env: Mapping[str, str]) -> QueenConfig:
    return QueenConfig(
        bot_token=env["SKEP_BOT_TOKEN"],
        owner_id=int(env["SKEP_OWNER_ID"]),
        group_chat_id=int(env["SKEP_GROUP_CHAT_ID"]),
        listen_host=env.get("SKEP_LISTEN_HOST", "0.0.0.0"),
        listen_port=int(env.get("SKEP_LISTEN_PORT", "8765")),
        shared_secret=env.get("SKEP_SHARED_SECRET", ""),
        public_url=env.get("SKEP_PUBLIC_URL"),
        advertise_mdns=_as_bool(env.get("SKEP_ADVERTISE_MDNS"), True),
        bookkeeping_db=env.get("SKEP_QUEEN_DB", "queen.sqlite"),
    )
```

Update `load_worker_config`:

```python
def load_worker_config(env: Mapping[str, str]) -> WorkerConfig:
    return WorkerConfig(
        host=env.get("SKEP_HOST") or socket.gethostname(),
        profile=env.get("SKEP_PROFILE", "default"),
        claude_config_dir=env.get("SKEP_CLAUDE_CONFIG_DIR"),
        repos_root=Path(env["SKEP_REPOS_ROOT"]),
        worktrees_root=Path(env["SKEP_WORKTREES_ROOT"]),
        db_path=env["SKEP_DB"],
        max_concurrent=int(env.get("SKEP_MAX_CONCURRENT", "8")),
        queen_url=env.get("SKEP_QUEEN_URL"),
        shared_secret=env.get("SKEP_SHARED_SECRET", ""),
        use_mdns=_as_bool(env.get("SKEP_USE_MDNS"), True),
        claude_bin=env.get("SKEP_CLAUDE_BIN", "claude"),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -q && uvx pyright src`
Expected: PASS; pyright reports 0 errors.

- [ ] **Step 5: Commit**

```bash
git add src/skep/config.py tests/test_config.py
git commit -m "feat(config): network + shared-secret fields for queen/worker"
```

---

## Task 2: Wire codec (`wire.py`)

**Files:**
- Create: `src/skep/wire.py`
- Test: `tests/test_wire.py`

**Interfaces:**
- Produces (all in `skep.wire`):
  - Type tags (str constants): `REGISTER`, `HEARTBEAT`, `TASK_STARTED`, `ACTIVITY`, `MILESTONE`, `DONE`, `LS_REPLY`, `SPAWN_REJECTED`, `SPAWN`, `KILL`, `PANIC`, `LS_REQUEST`.
  - `encode(msg: dict[str, Any]) -> str`, `decode(raw: str) -> dict[str, Any]` (raises `ValueError` if the payload is not a dict carrying a `"t"` tag).
  - Builders returning `{"t": <tag>, ...}` dicts: `register_msg(host, profile, version, active_tasks)`, `heartbeat_msg(active_tasks, capacity_remaining)`, `task_started_msg(local_id, repo, title)`, `activity_msg(local_id, line)`, `milestone_msg(local_id, text)`, `done_msg(local_id, status, summary)`, `spawn_rejected_msg(reason)`, `spawn_msg(repo, task)`, `kill_msg(task_id)`, `panic_msg()`, `ls_request_msg()`.
  - Each `active_tasks` entry is `{"local_id": int, "repo": str, "title": str}`.

- [ ] **Step 1: Write the failing test `tests/test_wire.py`**

```python
import pytest

from skep import wire


def test_encode_decode_roundtrip():
    msg = wire.task_started_msg(7, "nix", "clean nvidia")
    assert wire.decode(wire.encode(msg)) == msg
    assert msg["t"] == wire.TASK_STARTED


def test_register_msg_shape():
    msg = wire.register_msg("g16", "work", "0.1.0",
                            [{"local_id": 1, "repo": "nix", "title": "t"}])
    assert msg == {
        "t": wire.REGISTER, "host": "g16", "profile": "work",
        "version": "0.1.0",
        "active_tasks": [{"local_id": 1, "repo": "nix", "title": "t"}],
    }


def test_all_builders_carry_a_tag():
    builders = [
        wire.heartbeat_msg([], 8),
        wire.activity_msg(1, "x"),
        wire.milestone_msg(1, "m"),
        wire.done_msg(1, "done", "ok"),
        wire.spawn_rejected_msg("at capacity"),
        wire.spawn_msg("nix", "task"),
        wire.kill_msg(3),
        wire.panic_msg(),
        wire.ls_request_msg(),
    ]
    for b in builders:
        assert "t" in b
        assert wire.decode(wire.encode(b)) == b


def test_decode_rejects_non_dict():
    with pytest.raises(ValueError):
        wire.decode("[1, 2, 3]")


def test_decode_rejects_missing_tag():
    with pytest.raises(ValueError):
        wire.decode('{"local_id": 1}')
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_wire.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'skep.wire'`).

- [ ] **Step 3: Implement `src/skep/wire.py`**

```python
from __future__ import annotations

import json
from typing import Any

REGISTER = "register"
HEARTBEAT = "heartbeat"
TASK_STARTED = "task_started"
ACTIVITY = "activity"
MILESTONE = "milestone"
DONE = "done"
LS_REPLY = "ls_reply"
SPAWN_REJECTED = "spawn_rejected"
SPAWN = "spawn"
KILL = "kill"
PANIC = "panic"
LS_REQUEST = "ls_request"


def encode(msg: dict[str, Any]) -> str:
    return json.dumps(msg, separators=(",", ":"))


def decode(raw: str) -> dict[str, Any]:
    obj = json.loads(raw)
    if not isinstance(obj, dict) or "t" not in obj:
        raise ValueError(f"malformed message: {raw!r}")
    return obj


def register_msg(host: str, profile: str, version: str,
                 active_tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"t": REGISTER, "host": host, "profile": profile,
            "version": version, "active_tasks": active_tasks}


def heartbeat_msg(active_tasks: list[dict[str, Any]],
                  capacity_remaining: int) -> dict[str, Any]:
    return {"t": HEARTBEAT, "active_tasks": active_tasks,
            "capacity_remaining": capacity_remaining}


def task_started_msg(local_id: int, repo: str, title: str) -> dict[str, Any]:
    return {"t": TASK_STARTED, "local_id": local_id, "repo": repo, "title": title}


def activity_msg(local_id: int, line: str) -> dict[str, Any]:
    return {"t": ACTIVITY, "local_id": local_id, "line": line}


def milestone_msg(local_id: int, text: str) -> dict[str, Any]:
    return {"t": MILESTONE, "local_id": local_id, "text": text}


def done_msg(local_id: int, status: str, summary: str) -> dict[str, Any]:
    return {"t": DONE, "local_id": local_id, "status": status, "summary": summary}


def spawn_rejected_msg(reason: str) -> dict[str, Any]:
    return {"t": SPAWN_REJECTED, "reason": reason}


def spawn_msg(repo: str, task: str) -> dict[str, Any]:
    return {"t": SPAWN, "repo": repo, "task": task}


def kill_msg(task_id: int) -> dict[str, Any]:
    return {"t": KILL, "task_id": task_id}


def panic_msg() -> dict[str, Any]:
    return {"t": PANIC}


def ls_request_msg() -> dict[str, Any]:
    return {"t": LS_REQUEST}
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_wire.py -q && uvx pyright src`
Expected: PASS; pyright 0 errors.

- [ ] **Step 5: Commit**

```bash
git add src/skep/wire.py tests/test_wire.py
git commit -m "feat(wire): JSON message codec + typed builders"
```

---

## Task 3: Mutual auth handshake (`auth.py`)

**Files:**
- Create: `src/skep/auth.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Produces (all in `skep.auth`):
  - `AuthError(Exception)`.
  - Type aliases `Send = Callable[[dict[str, Any]], Awaitable[None]]`, `Recv = Callable[[], Awaitable[dict[str, Any]]]`.
  - `async def handshake_server(send: Send, recv: Recv, secret: str, *, nonce_factory: Callable[[], str] = ...) -> None`
  - `async def handshake_client(send: Send, recv: Recv, secret: str, *, nonce_factory: Callable[[], str] = ...) -> None`
  - Both raise `AuthError` on any protocol/proof mismatch. `nonce_factory` defaults to `secrets.token_hex` (injectable for deterministic tests).
- Protocol: server sends `{"t":"challenge","nonce":<sn>}`; client replies `{"t":"auth","nonce":<cn>,"proof":HMAC(secret, sn:cn)}`; server verifies then replies `{"t":"auth_ok","proof":HMAC(secret, cn:sn)}`; client verifies. Fresh nonces per handshake defeat replay.

- [ ] **Step 1: Write the failing test `tests/test_auth.py`**

```python
import asyncio

import pytest

from skep.auth import AuthError, handshake_client, handshake_server


def _pipe():
    """Two async queues wired as a bidirectional dict channel."""
    a: asyncio.Queue = asyncio.Queue()
    b: asyncio.Queue = asyncio.Queue()

    async def send_a(m):
        await a.put(m)

    async def recv_a():
        return await b.get()

    async def send_b(m):
        await b.put(m)

    async def recv_b():
        return await a.get()

    # server uses send_a/recv_a; client uses send_b/recv_b
    return (send_a, recv_a), (send_b, recv_b)


async def test_handshake_succeeds_with_matching_secret():
    (s_send, s_recv), (c_send, c_recv) = _pipe()
    await asyncio.gather(
        handshake_server(s_send, s_recv, "secret"),
        handshake_client(c_send, c_recv, "secret"),
    )  # no exception == success


async def test_server_rejects_wrong_client_secret():
    (s_send, s_recv), (c_send, c_recv) = _pipe()
    results = await asyncio.gather(
        handshake_server(s_send, s_recv, "right"),
        handshake_client(c_send, c_recv, "wrong"),
        return_exceptions=True,
    )
    assert any(isinstance(r, AuthError) for r in results)


async def test_client_rejects_wrong_server_secret():
    (s_send, s_recv), (c_send, c_recv) = _pipe()
    results = await asyncio.gather(
        handshake_server(s_send, s_recv, "wrong"),
        handshake_client(c_send, c_recv, "right"),
        return_exceptions=True,
    )
    client_result = results[1]
    assert isinstance(client_result, AuthError)


async def test_replayed_client_proof_is_rejected():
    # Capture a valid client `auth` frame produced against server nonce "N1".
    captured: dict = {}

    async def s_send(m):
        pass

    async def s_recv():
        raise AssertionError("not used")

    async def c_send(m):
        captured.update(m)

    async def c_recv():
        return {"t": "challenge", "nonce": "N1"}

    await handshake_client(c_send, c_recv, "secret",
                           nonce_factory=lambda: "CN")
    assert captured["t"] == "auth"

    # A fresh server issues a DIFFERENT nonce "N2"; replaying the old proof fails.
    sent: list = []

    async def s2_send(m):
        sent.append(m)

    async def s2_recv():
        return captured  # replayed stale auth frame

    with pytest.raises(AuthError):
        await handshake_server(s2_send, s2_recv, "secret",
                               nonce_factory=lambda: "N2")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_auth.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'skep.auth'`).

- [ ] **Step 3: Implement `src/skep/auth.py`**

```python
from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

Send = Callable[[dict[str, Any]], Awaitable[None]]
Recv = Callable[[], Awaitable[dict[str, Any]]]


class AuthError(Exception):
    """Raised when the challenge-response handshake fails on either side."""


def _proof(secret: str, first: str, second: str) -> str:
    return hmac.new(secret.encode(), f"{first}:{second}".encode(),
                    hashlib.sha256).hexdigest()


async def handshake_server(
    send: Send, recv: Recv, secret: str, *,
    nonce_factory: Callable[[], str] = secrets.token_hex,
) -> None:
    server_nonce = nonce_factory()
    await send({"t": "challenge", "nonce": server_nonce})
    msg = await recv()
    if msg.get("t") != "auth":
        raise AuthError("expected auth frame")
    client_nonce = str(msg.get("nonce", ""))
    expected = _proof(secret, server_nonce, client_nonce)
    if not hmac.compare_digest(str(msg.get("proof", "")), expected):
        raise AuthError("bad client proof")
    await send({"t": "auth_ok", "proof": _proof(secret, client_nonce, server_nonce)})


async def handshake_client(
    send: Send, recv: Recv, secret: str, *,
    nonce_factory: Callable[[], str] = secrets.token_hex,
) -> None:
    msg = await recv()
    if msg.get("t") != "challenge":
        raise AuthError("expected challenge frame")
    server_nonce = str(msg.get("nonce", ""))
    client_nonce = nonce_factory()
    await send({"t": "auth", "nonce": client_nonce,
                "proof": _proof(secret, server_nonce, client_nonce)})
    reply = await recv()
    if reply.get("t") != "auth_ok":
        raise AuthError("expected auth_ok frame")
    expected = _proof(secret, client_nonce, server_nonce)
    if not hmac.compare_digest(str(reply.get("proof", "")), expected):
        raise AuthError("bad server proof")
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_auth.py -q && uvx pyright src`
Expected: PASS; pyright 0 errors.

- [ ] **Step 5: Commit**

```bash
git add src/skep/auth.py tests/test_auth.py
git commit -m "feat(auth): mutual HMAC challenge-response handshake"
```

---

## Task 4: Queen WS server + `RemoteWorker` + `on_spawn_rejected`

Adds the aiohttp dependency, extends the `QueenInbox` seam with `on_spawn_rejected`, and builds the queen's server half: accept a connection, run the server handshake, read `register`, register a `RemoteWorker` command handler in the router, and pump inbound event frames into the `QueenInbox`.

**Files:**
- Modify: `pyproject.toml` (add `aiohttp`)
- Modify: `src/skep/transport.py` (`QueenInbox` += `on_spawn_rejected`)
- Modify: `src/skep/queen/telegram_sink.py` (implement `on_spawn_rejected`)
- Modify: `src/skep/queen/router.py` (add `unregister`)
- Create: `src/skep/ws_transport.py` (`RemoteWorker`, `QueenWsServer`)
- Test: `tests/test_ws_transport.py`

**Interfaces:**
- Consumes: `skep.wire`, `skep.auth`, `QueenRouter.register/unregister`, `QueenInbox`.
- Produces:
  - `QueenInbox.on_spawn_rejected(self, host: str, profile: str, reason: str) -> None`.
  - `RemoteWorker(ws: web.WebSocketResponse)` implementing `CommandHandler` — `spawn`→sends `spawn_msg` returns `0`; `kill`→sends `kill_msg` returns `True`; `panic`→sends `panic_msg` returns `1`.
  - `QueenWsServer(router: QueenRouter, inbox: QueenInbox, secret: str, *, heartbeat: float = 20.0)` with `attach(app: web.Application, path: str = "/ws") -> None`.
  - `QueenSink.on_spawn_rejected` posts a plain (escaped) notice to the control group.

- [ ] **Step 1: Add `aiohttp` to `pyproject.toml`**

```toml
dependencies = ["aiogram>=3.13", "aiohttp>=3.10"]
```

Run: `uv sync`

- [ ] **Step 2: Extend the seam + router (no test yet — supporting change)**

In `src/skep/transport.py`, add to the `QueenInbox` Protocol (after `on_done`):

```python
    async def on_spawn_rejected(self, host: str, profile: str,
                                reason: str) -> None: ...
```

In `src/skep/queen/router.py`, add an `unregister` method to `QueenRouter`:

```python
    def unregister(self, host: str, profile: str) -> None:
        self._workers.pop((host, profile), None)
```

In `src/skep/queen/telegram_sink.py`, implement `on_spawn_rejected` on `QueenSink`:

```python
    async def on_spawn_rejected(self, host: str, profile: str,
                                reason: str) -> None:
        text = escape_md(f"spawn on {host}/{profile} rejected: {reason}")
        await self._gw.post(0, text)
```

> Topic `0` = the group's General topic (Gateway.post targets a topic id; `0`/None is the default topic). If `Gateway.post` requires a real topic id, post via `self._gw.post(self._gw._config.group_chat_id ...)` is wrong — use the General topic convention already used elsewhere. If unsure, confirm `Gateway.post` signature; the intent is "notify the control group, not a per-task topic".

- [ ] **Step 3: Write the failing test `tests/test_ws_transport.py`**

```python
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
import aiohttp

from skep import wire
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.router import QueenRouter
from skep.ws_transport import QueenWsServer


class RecordingInbox:
    def __init__(self):
        self.events: list[tuple] = []

    async def on_task_started(self, host, profile, local_id, repo, title):
        self.events.append(("task_started", host, profile, local_id, repo, title))

    async def on_activity(self, host, profile, local_id, line):
        self.events.append(("activity", host, profile, local_id, line))

    async def on_milestone(self, host, profile, local_id, text):
        self.events.append(("milestone", host, profile, local_id, text))

    async def on_done(self, host, profile, local_id, status, summary):
        self.events.append(("done", host, profile, local_id, status, summary))

    async def on_spawn_rejected(self, host, profile, reason):
        self.events.append(("spawn_rejected", host, profile, reason))


async def _serve(router, inbox, secret="s"):
    app = web.Application()
    QueenWsServer(router, inbox, secret).attach(app)
    server = TestServer(app)
    await server.start_server()
    return server, f"ws://127.0.0.1:{server.port}/ws"


async def _client_handshake(ws, secret="s"):
    from skep.auth import handshake_client

    async def send(m):
        await ws.send_str(wire.encode(m))

    async def recv():
        msg = await ws.receive()
        return wire.decode(msg.data)

    await handshake_client(send, recv, secret)


async def test_register_then_event_reaches_inbox():
    inbox = RecordingInbox()
    router = QueenRouter(Bookkeeping.open(":memory:"))
    server, url = await _serve(router, inbox)
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(url) as ws:
                await _client_handshake(ws)
                await ws.send_str(wire.encode(
                    wire.register_msg("g16", "work", "0.1.0", [])))
                await ws.send_str(wire.encode(
                    wire.task_started_msg(1, "nix", "clean")))
                await ws.send_str(wire.encode(wire.activity_msg(1, "hi")))
                for _ in range(100):
                    if len(inbox.events) >= 2:
                        break
                    await asyncio.sleep(0.01)
    finally:
        await server.close()
    assert ("task_started", "g16", "work", 1, "nix", "clean") in inbox.events
    assert ("activity", "g16", "work", 1, "hi") in inbox.events


async def test_register_makes_worker_routable():
    inbox = RecordingInbox()
    router = QueenRouter(Bookkeeping.open(":memory:"))
    server, url = await _serve(router, inbox)
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(url) as ws:
                await _client_handshake(ws)
                await ws.send_str(wire.encode(
                    wire.register_msg("g16", "work", "0.1.0", [])))
                # wait until routable
                for _ in range(100):
                    try:
                        await router.cmd_spawn("g16", "work", "nix", "task")
                        break
                    except Exception:
                        await asyncio.sleep(0.01)
                # queen -> worker command frame should arrive
                got = wire.decode((await ws.receive()).data)
                assert got == wire.spawn_msg("nix", "task")
    finally:
        await server.close()


async def test_wrong_secret_is_rejected():
    inbox = RecordingInbox()
    router = QueenRouter(Bookkeeping.open(":memory:"))
    server, url = await _serve(router, inbox, secret="right")
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(url) as ws:
                from skep.auth import AuthError
                with pytest.raises(AuthError):
                    await _client_handshake(ws, secret="wrong")
    finally:
        await server.close()
```

- [ ] **Step 4: Run to verify it fails**

Run: `uv run pytest tests/test_ws_transport.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'skep.ws_transport'`).

- [ ] **Step 5: Implement `src/skep/ws_transport.py` (queen half)**

```python
from __future__ import annotations

from typing import Any

from aiohttp import web

from skep import wire
from skep.auth import AuthError, handshake_server
from skep.queen.router import QueenRouter
from skep.transport import QueenInbox


class RemoteWorker:
    """A CommandHandler that forwards queen commands to one worker's socket."""

    def __init__(self, ws: web.WebSocketResponse):
        self._ws = ws

    async def spawn(self, repo: str, task: str) -> int:
        await self._ws.send_str(wire.encode(wire.spawn_msg(repo, task)))
        return 0

    async def kill(self, task_id: int) -> bool:
        await self._ws.send_str(wire.encode(wire.kill_msg(task_id)))
        return True

    async def panic(self) -> int:
        await self._ws.send_str(wire.encode(wire.panic_msg()))
        return 1


class QueenWsServer:
    def __init__(self, router: QueenRouter, inbox: QueenInbox, secret: str,
                 *, heartbeat: float = 20.0):
        self._router = router
        self._inbox = inbox
        self._secret = secret
        self._heartbeat = heartbeat

    def attach(self, app: web.Application, path: str = "/ws") -> None:
        app.router.add_get(path, self._handle)

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=self._heartbeat)
        await ws.prepare(request)

        async def send(m: dict[str, Any]) -> None:
            await ws.send_str(wire.encode(m))

        async def recv() -> dict[str, Any]:
            msg = await ws.receive()
            if msg.type != web.WSMsgType.TEXT:
                raise AuthError("connection closed during handshake")
            return wire.decode(msg.data)

        try:
            await handshake_server(send, recv, self._secret)
            reg = await recv()
        except (AuthError, ValueError):
            await ws.close()
            return ws
        if reg.get("t") != wire.REGISTER:
            await ws.close()
            return ws

        host = str(reg["host"])
        profile = str(reg["profile"])
        self._router.register(host, profile, RemoteWorker(ws))
        self._router.mark_online(host, profile)
        for t in reg.get("active_tasks", []):
            await self._inbox.on_task_started(
                host, profile, int(t["local_id"]), str(t["repo"]), str(t["title"]))

        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                await self._dispatch(host, profile, wire.decode(msg.data))
        finally:
            self._router.mark_offline(host, profile)
            self._router.unregister(host, profile)
        return ws

    async def _dispatch(self, host: str, profile: str,
                        msg: dict[str, Any]) -> None:
        t = msg.get("t")
        if t == wire.HEARTBEAT:
            self._router.touch(host, profile)
        elif t == wire.TASK_STARTED:
            await self._inbox.on_task_started(
                host, profile, int(msg["local_id"]), str(msg["repo"]), str(msg["title"]))
        elif t == wire.ACTIVITY:
            await self._inbox.on_activity(
                host, profile, int(msg["local_id"]), str(msg["line"]))
        elif t == wire.MILESTONE:
            await self._inbox.on_milestone(
                host, profile, int(msg["local_id"]), str(msg["text"]))
        elif t == wire.DONE:
            await self._inbox.on_done(
                host, profile, int(msg["local_id"]),
                str(msg["status"]), str(msg["summary"]))
        elif t == wire.SPAWN_REJECTED:
            await self._inbox.on_spawn_rejected(host, profile, str(msg["reason"]))
```

> `mark_online` / `mark_offline` / `touch` are added to `QueenRouter` in **Task 6**. To keep this task green in isolation, add **stub** methods now (they become real in Task 6):
>
> ```python
>     def mark_online(self, host: str, profile: str) -> None: ...
>     def mark_offline(self, host: str, profile: str) -> None: ...
>     def touch(self, host: str, profile: str) -> None: ...
> ```
>
> Add these three stubs to `QueenRouter` in this task so the server compiles; Task 6 replaces the bodies.

- [ ] **Step 6: Run to verify it passes**

Run: `uv run pytest tests/test_ws_transport.py tests/test_router.py tests/test_telegram_sink.py -q && uvx pyright src`
Expected: PASS; pyright 0 errors.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/skep/ws_transport.py src/skep/transport.py \
        src/skep/queen/router.py src/skep/queen/telegram_sink.py tests/test_ws_transport.py
git commit -m "feat(ws): queen WS server + RemoteWorker + on_spawn_rejected"
```

---

## Task 5: Worker WS client + `WsEventSink` + `SwitchableEventSink`

Builds the worker's client half: connect out, handshake, `register` with active tasks, expose a `WsEventSink` for the Supervisor, and dispatch inbound command frames to the Supervisor — surfacing `CapacityError` as a `spawn_rejected` frame. Introduces `SwitchableEventSink` so the Supervisor's sink survives reconnects.

**Files:**
- Modify: `src/skep/transport.py` (add `SwitchableEventSink`)
- Modify: `src/skep/ws_transport.py` (add `WsEventSink`, `WorkerWsClient.run_once`)
- Test: `tests/test_transport.py` (SwitchableEventSink), `tests/test_ws_transport.py` (client)

**Interfaces:**
- Consumes: `QueenWsServer` (Task 4), `Supervisor` (a `CommandHandler` + `.list_active()`), `CapacityError`.
- Produces:
  - `SwitchableEventSink()` implementing `EventSink`; attribute `target: EventSink | None` (default `None`); each method forwards to `target` if set, else no-op.
  - `WsEventSink(ws: aiohttp.ClientWebSocketResponse)` implementing `EventSink` over the wire.
  - `WORKER_VERSION: str` constant in `ws_transport`.
  - `WorkerWsClient(config: WorkerConfig, supervisor: CommandHandler, switch: SwitchableEventSink, secret: str, *, heartbeat: float = 20.0)` with `async def run_once(self, session: aiohttp.ClientSession, url: str) -> None` — one full connection lifecycle (handshake → register → concurrent command-receive loop; sets/clears `switch.target`). `supervisor` must also expose `list_active() -> list[Task]` for the register payload; type it as `Supervisor` to access both.

- [ ] **Step 1: Write the failing `SwitchableEventSink` test (append to `tests/test_transport.py`)**

```python
from skep.transport import SwitchableEventSink


class _Rec:
    def __init__(self):
        self.calls = []

    async def task_started(self, local_id, repo, title):
        self.calls.append(("task_started", local_id, repo, title))

    async def activity(self, local_id, line):
        self.calls.append(("activity", local_id, line))

    async def milestone(self, local_id, text):
        self.calls.append(("milestone", local_id, text))

    async def done(self, local_id, status, summary):
        self.calls.append(("done", local_id, status, summary))


async def test_switchable_forwards_to_target():
    rec = _Rec()
    s = SwitchableEventSink()
    s.target = rec
    await s.task_started(1, "nix", "t")
    assert rec.calls == [("task_started", 1, "nix", "t")]


async def test_switchable_drops_when_detached():
    s = SwitchableEventSink()
    s.target = None
    await s.activity(1, "line")  # no target -> no error, dropped
    await s.done(1, "done", "ok")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_transport.py -q`
Expected: FAIL (`ImportError: cannot import name 'SwitchableEventSink'`).

- [ ] **Step 3: Implement `SwitchableEventSink` in `src/skep/transport.py`**

```python
class SwitchableEventSink:
    """A stable EventSink the Supervisor holds; its target is swapped per WS
    connection. When target is None (worker detached) events are dropped —
    agents keep running, only reporting pauses (design §6.4)."""

    def __init__(self) -> None:
        self.target: EventSink | None = None

    async def task_started(self, local_id: int, repo: str, title: str) -> None:
        if self.target is not None:
            await self.target.task_started(local_id, repo, title)

    async def activity(self, local_id: int, line: str) -> None:
        if self.target is not None:
            await self.target.activity(local_id, line)

    async def milestone(self, local_id: int, text: str) -> None:
        if self.target is not None:
            await self.target.milestone(local_id, text)

    async def done(self, local_id: int, status: str, summary: str) -> None:
        if self.target is not None:
            await self.target.done(local_id, status, summary)
```

- [ ] **Step 4: Write the failing client test (append to `tests/test_ws_transport.py`)**

```python
from skep.config import WorkerConfig
from skep.transport import SwitchableEventSink
from skep.ws_transport import WorkerWsClient


class FakeSupervisor:
    """Stands in for Supervisor as a CommandHandler + list_active source."""

    def __init__(self, capacity_ok=True):
        self.spawned: list[tuple[str, str]] = []
        self.killed: list[int] = []
        self.panics = 0
        self._capacity_ok = capacity_ok

    def list_active(self):
        return []

    async def spawn(self, repo, task):
        from skep.supervisor import CapacityError
        if not self._capacity_ok:
            raise CapacityError("at capacity (0 running)")
        self.spawned.append((repo, task))
        return 1

    async def kill(self, task_id):
        self.killed.append(task_id)
        return True

    async def panic(self):
        self.panics += 1
        return 0


def _wcfg(url):
    from pathlib import Path
    return WorkerConfig(
        host="g16", profile="work", claude_config_dir=None,
        repos_root=Path("/tmp"), worktrees_root=Path("/tmp"),
        db_path=":memory:", queen_url=url, shared_secret="s",
    )


async def test_worker_client_dispatches_spawn_command():
    inbox = RecordingInbox()
    router = QueenRouter(Bookkeeping.open(":memory:"))
    server, url = await _serve(router, inbox)
    sup = FakeSupervisor()
    switch = SwitchableEventSink()
    client = WorkerWsClient(_wcfg(url), sup, switch, secret="s")
    try:
        async with aiohttp.ClientSession() as sess:
            task = asyncio.create_task(client.run_once(sess, url))
            # queen waits for the worker to register, then spawns
            for _ in range(100):
                try:
                    await router.cmd_spawn("g16", "work", "nix", "clean")
                    break
                except Exception:
                    await asyncio.sleep(0.01)
            for _ in range(100):
                if sup.spawned:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
    finally:
        await server.close()
    assert sup.spawned == [("nix", "clean")]


async def test_worker_client_reports_capacity_rejection():
    inbox = RecordingInbox()
    router = QueenRouter(Bookkeeping.open(":memory:"))
    server, url = await _serve(router, inbox)
    sup = FakeSupervisor(capacity_ok=False)
    switch = SwitchableEventSink()
    client = WorkerWsClient(_wcfg(url), sup, switch, secret="s")
    try:
        async with aiohttp.ClientSession() as sess:
            task = asyncio.create_task(client.run_once(sess, url))
            for _ in range(100):
                try:
                    await router.cmd_spawn("g16", "work", "nix", "clean")
                    break
                except Exception:
                    await asyncio.sleep(0.01)
            for _ in range(100):
                if any(e[0] == "spawn_rejected" for e in inbox.events):
                    break
                await asyncio.sleep(0.01)
            task.cancel()
    finally:
        await server.close()
    assert any(e[0] == "spawn_rejected" for e in inbox.events)
```

- [ ] **Step 5: Run to verify it fails**

Run: `uv run pytest tests/test_ws_transport.py -q`
Expected: FAIL (`ImportError: cannot import name 'WorkerWsClient'`).

- [ ] **Step 6: Implement the worker client in `src/skep/ws_transport.py`**

Add imports at the top:

```python
import aiohttp
from collections.abc import Awaitable, Callable

from skep.config import WorkerConfig
from skep.supervisor import CapacityError, Supervisor
from skep.transport import SwitchableEventSink

WORKER_VERSION = "0.1.0"
```

Add `WsEventSink` and `WorkerWsClient`:

```python
class WsEventSink:
    """EventSink that serialises each domain event to a JSON frame."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse):
        self._ws = ws

    async def task_started(self, local_id: int, repo: str, title: str) -> None:
        await self._ws.send_str(wire.encode(wire.task_started_msg(local_id, repo, title)))

    async def activity(self, local_id: int, line: str) -> None:
        await self._ws.send_str(wire.encode(wire.activity_msg(local_id, line)))

    async def milestone(self, local_id: int, text: str) -> None:
        await self._ws.send_str(wire.encode(wire.milestone_msg(local_id, text)))

    async def done(self, local_id: int, status: str, summary: str) -> None:
        await self._ws.send_str(wire.encode(wire.done_msg(local_id, status, summary)))


class WorkerWsClient:
    def __init__(self, config: WorkerConfig, supervisor: Supervisor,
                 switch: SwitchableEventSink, secret: str,
                 *, heartbeat: float = 20.0):
        self._cfg = config
        self._sup = supervisor
        self._switch = switch
        self._secret = secret
        self._heartbeat = heartbeat

    def _active_payload(self) -> list[dict[str, Any]]:
        return [{"local_id": t.id, "repo": t.repo, "title": t.task}
                for t in self._sup.list_active() if t.id is not None]

    async def run_once(self, session: aiohttp.ClientSession, url: str) -> None:
        from skep.auth import handshake_client
        async with session.ws_connect(url, heartbeat=self._heartbeat) as ws:
            async def send(m: dict[str, Any]) -> None:
                await ws.send_str(wire.encode(m))

            async def recv() -> dict[str, Any]:
                msg = await ws.receive()
                if msg.type != web.WSMsgType.TEXT:
                    raise AuthError("connection closed during handshake")
                return wire.decode(msg.data)

            await handshake_client(send, recv, self._secret)
            await send(wire.register_msg(
                self._cfg.host, self._cfg.profile, WORKER_VERSION,
                self._active_payload()))
            self._switch.target = WsEventSink(ws)
            try:
                async for msg in ws:
                    if msg.type != web.WSMsgType.TEXT:
                        continue
                    await self._on_command(ws, wire.decode(msg.data))
            finally:
                self._switch.target = None

    async def _on_command(self, ws: aiohttp.ClientWebSocketResponse,
                          msg: dict[str, Any]) -> None:
        t = msg.get("t")
        if t == wire.SPAWN:
            try:
                await self._sup.spawn(str(msg["repo"]), str(msg["task"]))
            except CapacityError as exc:
                await ws.send_str(wire.encode(wire.spawn_rejected_msg(str(exc))))
        elif t == wire.KILL:
            await self._sup.kill(int(msg["task_id"]))
        elif t == wire.PANIC:
            await self._sup.panic()
```

- [ ] **Step 7: Run to verify it passes**

Run: `uv run pytest tests/test_transport.py tests/test_ws_transport.py -q && uvx pyright src`
Expected: PASS; pyright 0 errors.

- [ ] **Step 8: Commit**

```bash
git add src/skep/transport.py src/skep/ws_transport.py \
        tests/test_transport.py tests/test_ws_transport.py
git commit -m "feat(ws): worker WS client + WsEventSink + SwitchableEventSink"
```

---

## Task 6: Heartbeat + presence + detached `/ls`

Replaces the Task-4 router stubs with real presence tracking, sends the application-level heartbeat from the worker, and renders online/detached state in `/ls`. Enforces the correctness invariant: a disconnect never marks tasks dead.

**Files:**
- Modify: `src/skep/queen/router.py` (real presence + `format_ls` marker)
- Modify: `src/skep/ws_transport.py` (`WorkerWsClient` heartbeat loop)
- Test: `tests/test_router.py`, `tests/test_ws_transport.py`

**Interfaces:**
- Produces:
  - `QueenRouter(bookkeeping, *, now: Callable[[], float] = time.monotonic)`.
  - `mark_online(host, profile)` (adds to online set, records `last_seen`), `mark_offline(host, profile)` (removes from online set, keeps `last_seen`), `touch(host, profile)` (updates `last_seen`), `is_online(host, profile) -> bool`.
  - `format_ls()` appends ` (detached)` to rows whose `(host, profile)` is not online.
- Behavior: `WorkerWsClient` starts a background task that sends `heartbeat_msg(active_tasks, capacity_remaining)` every `heartbeat` seconds while connected.

- [ ] **Step 1: Write failing presence tests (append to `tests/test_router.py`)**

```python
def test_presence_online_offline_touch():
    r = QueenRouter(Bookkeeping.open(":memory:"), now=lambda: 100.0)
    assert r.is_online("g16", "work") is False
    r.mark_online("g16", "work")
    assert r.is_online("g16", "work") is True
    r.mark_offline("g16", "work")
    assert r.is_online("g16", "work") is False


async def test_format_ls_marks_detached():
    bk = Bookkeeping.open(":memory:")
    bk.add("g16", "work", 1, "nix", "clean", topic_id=5)
    r = QueenRouter(bk)
    # not online -> detached
    assert "(detached)" in r.format_ls()
    r.mark_online("g16", "work")
    assert "(detached)" not in r.format_ls()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_router.py -q`
Expected: FAIL (stub `mark_online` does nothing / `is_online` missing).

- [ ] **Step 3: Implement real presence in `src/skep/queen/router.py`**

Add `import time` and `from collections.abc import Callable` at the top. Replace the Task-4 stubs and `__init__`:

```python
class QueenRouter:
    def __init__(self, bookkeeping: Bookkeeping, *,
                 now: Callable[[], float] = time.monotonic):
        self._bk = bookkeeping
        self._workers: dict[tuple[str, str], CommandHandler] = {}
        self._online: set[tuple[str, str]] = set()
        self._last_seen: dict[tuple[str, str], float] = {}
        self._now = now

    def mark_online(self, host: str, profile: str) -> None:
        self._online.add((host, profile))
        self._last_seen[(host, profile)] = self._now()

    def mark_offline(self, host: str, profile: str) -> None:
        self._online.discard((host, profile))

    def touch(self, host: str, profile: str) -> None:
        self._last_seen[(host, profile)] = self._now()

    def is_online(self, host: str, profile: str) -> bool:
        return (host, profile) in self._online
```

Update `format_ls` to append the marker:

```python
    def format_ls(self) -> str:
        entries = self._bk.list_active()
        if not entries:
            return "No active agents\\."
        lines = []
        for e in entries:
            marker = "" if self.is_online(e.host, e.profile) else " \\(detached\\)"
            lines.append(
                f"`{e.ref}` {escape_md(e.host)}/{escape_md(e.profile)} "
                f"{escape_md(e.repo)} — {escape_md(e.status)}{marker}"
            )
        return "\n".join(lines)
```

> The `(detached)` marker is MarkdownV2, so parentheses are escaped as `\(detached\)`. The test asserts the substring `(detached)` which is present within `\(detached\)`.

- [ ] **Step 4: Write the failing heartbeat test (append to `tests/test_ws_transport.py`)**

```python
async def test_worker_sends_heartbeat():
    inbox = RecordingInbox()
    router = QueenRouter(Bookkeeping.open(":memory:"))
    server, url = await _serve(router, inbox)
    sup = FakeSupervisor()
    switch = SwitchableEventSink()
    client = WorkerWsClient(_wcfg(url), sup, switch, secret="s", heartbeat=0.05)
    seen = {"beat": False}

    # Wrap the inbox dispatch by observing router.touch via last_seen change.
    router.mark_online("g16", "work")  # baseline
    before = dict(router._last_seen)  # touch updates this on heartbeat

    try:
        async with aiohttp.ClientSession() as sess:
            task = asyncio.create_task(client.run_once(sess, url))
            for _ in range(100):
                if router._last_seen.get(("g16", "work"), 0) and \
                        router._last_seen != before:
                    seen["beat"] = True
                    break
                await asyncio.sleep(0.02)
            task.cancel()
    finally:
        await server.close()
    assert seen["beat"]
```

> This asserts the queen received at least one `heartbeat` frame (which calls `router.touch`, refreshing `last_seen`). If asserting on the private `_last_seen` is undesirable, add a `QueenRouter.last_seen(host, profile) -> float | None` accessor and assert on that instead.

- [ ] **Step 5: Run to verify it fails**

Run: `uv run pytest tests/test_ws_transport.py::test_worker_sends_heartbeat -q`
Expected: FAIL (no heartbeat sent yet).

- [ ] **Step 6: Add the heartbeat loop to `WorkerWsClient.run_once`**

Add a helper and start it after setting `switch.target`:

```python
    async def _heartbeat_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            await asyncio.sleep(self._heartbeat)
            active = self._active_payload()
            remaining = max(0, self._cfg.max_concurrent - len(active))
            await ws.send_str(wire.encode(wire.heartbeat_msg(active, remaining)))
```

In `run_once`, wrap the receive loop so the heartbeat runs concurrently and is cancelled on exit:

```python
            self._switch.target = WsEventSink(ws)
            hb = asyncio.create_task(self._heartbeat_loop(ws))
            try:
                async for msg in ws:
                    if msg.type != web.WSMsgType.TEXT:
                        continue
                    await self._on_command(ws, wire.decode(msg.data))
            finally:
                hb.cancel()
                self._switch.target = None
```

Add `import asyncio` to `ws_transport.py` if not already present.

- [ ] **Step 7: Run to verify it passes**

Run: `uv run pytest tests/test_router.py tests/test_ws_transport.py -q && uvx pyright src`
Expected: PASS; pyright 0 errors.

- [ ] **Step 8: Commit**

```bash
git add src/skep/queen/router.py src/skep/ws_transport.py \
        tests/test_router.py tests/test_ws_transport.py
git commit -m "feat(ws): app-level heartbeat + queen presence + detached /ls"
```

---

## Task 7: Reconnect / backoff + topic re-attachment

Adds the worker's reconnect loop (`WorkerWsClient.run`) with bounded backoff, and makes the queen's `on_task_started` idempotent so a re-registering worker re-attaches to its existing topic instead of creating a duplicate.

**Files:**
- Modify: `src/skep/queen/telegram_sink.py` (idempotent `on_task_started`)
- Modify: `src/skep/ws_transport.py` (`WorkerWsClient.run` reconnect loop)
- Test: `tests/test_telegram_sink.py`, `tests/test_ws_transport.py`

**Interfaces:**
- Produces:
  - `QueenSink.on_task_started` — if `bookkeeping.by_worker_task(host, profile, local_id)` already exists, return without creating a new topic (re-attach); else create + `add` as before.
  - `WorkerWsClient.run(self, *, max_backoff: float = 30.0, _once: bool = False) -> None` — loops `run_once` over a fresh `ClientSession`, sleeping with exponential backoff (0.5→`max_backoff`) between drops; resets backoff after a successful connection. `_once` (test hook) runs a single reconnect and returns.

- [ ] **Step 1: Write the failing idempotency test (append to `tests/test_telegram_sink.py`)**

```python
async def test_on_task_started_is_reattach_idempotent():
    from unittest.mock import AsyncMock, MagicMock
    from skep.queen.bookkeeping import Bookkeeping
    from skep.queen.telegram_sink import QueenSink

    gw = MagicMock()
    gw.create_topic = AsyncMock(return_value=100)
    gw.post = AsyncMock(return_value=1)
    bk = Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)

    await sink.on_task_started("g16", "work", 1, "nix", "clean")
    await sink.on_task_started("g16", "work", 1, "nix", "clean")  # re-register

    assert gw.create_topic.await_count == 1  # no duplicate topic
    assert bk.by_worker_task("g16", "work", 1) is not None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_telegram_sink.py::test_on_task_started_is_reattach_idempotent -q`
Expected: FAIL (`create_topic.await_count == 2`).

- [ ] **Step 3: Make `on_task_started` idempotent**

In `src/skep/queen/telegram_sink.py`:

```python
    async def on_task_started(self, host: str, profile: str, local_id: int,
                              repo: str, title: str) -> None:
        if self._bk.by_worker_task(host, profile, local_id) is not None:
            return  # re-attach: worker re-registered an already-known task
        topic_id = await self._gw.create_topic(f"{host}·{profile}·{repo}")
        self._bk.add(host, profile, local_id, repo, title, topic_id)
```

- [ ] **Step 4: Write the failing reconnect test (append to `tests/test_ws_transport.py`)**

```python
async def test_worker_reconnects_after_drop():
    inbox = RecordingInbox()
    router = QueenRouter(Bookkeeping.open(":memory:"))
    server, url = await _serve(router, inbox)
    sup = FakeSupervisor()
    switch = SwitchableEventSink()
    client = WorkerWsClient(_wcfg(url), sup, switch, secret="s")

    connects = {"n": 0}
    orig = client.run_once

    async def counting_run_once(session, u):
        connects["n"] += 1
        if connects["n"] == 1:
            raise ConnectionError("simulated drop")
        await orig(session, u)

    client.run_once = counting_run_once  # type: ignore[method-assign]
    try:
        task = asyncio.create_task(client.run(max_backoff=0.1))
        for _ in range(200):
            if connects["n"] >= 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
    finally:
        await server.close()
    assert connects["n"] >= 2  # dropped once, reconnected
```

- [ ] **Step 5: Run to verify it fails**

Run: `uv run pytest tests/test_ws_transport.py::test_worker_reconnects_after_drop -q`
Expected: FAIL (`WorkerWsClient` has no `run`).

- [ ] **Step 6: Implement the reconnect loop in `WorkerWsClient`**

```python
    async def run(self, *, max_backoff: float = 30.0, _once: bool = False) -> None:
        backoff = 0.5
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    url = self._cfg.queen_url or ""
                    await self.run_once(session, url)
                backoff = 0.5  # clean close -> reset
            except (aiohttp.ClientError, ConnectionError, AuthError, OSError):
                pass
            if _once:
                return
            await asyncio.sleep(backoff)
            backoff = min(max_backoff, backoff * 2)
```

> `run` resolves the URL from `config.queen_url`; the mDNS fallback (Task 8) is injected by the entrypoint (Task 10) by setting `queen_url` before calling `run`, or by passing a resolved URL. Keep `run` taking the URL from config here.

- [ ] **Step 7: Run to verify it passes**

Run: `uv run pytest tests/test_telegram_sink.py tests/test_ws_transport.py -q && uvx pyright src`
Expected: PASS; pyright 0 errors.

- [ ] **Step 8: Commit**

```bash
git add src/skep/queen/telegram_sink.py src/skep/ws_transport.py \
        tests/test_telegram_sink.py tests/test_ws_transport.py
git commit -m "feat(ws): worker reconnect/backoff + idempotent topic re-attach"
```

---

## Task 8: mDNS discovery (`discovery.py`)

Adds the `zeroconf` dependency and a discovery module: the queen advertises `_skep-queen._tcp.local.`; the worker browses+resolves it, with `--queen-url` as an explicit fallback. Discovered addresses are untrusted until the handshake completes (§9) — discovery only produces a URL, auth still gates every connection.

**Files:**
- Modify: `pyproject.toml` (add `zeroconf`)
- Create: `src/skep/discovery.py`
- Test: `tests/test_discovery.py`

**Interfaces:**
- Produces (all in `skep.discovery`):
  - `SERVICE_TYPE = "_skep-queen._tcp.local."`
  - `async def advertise(host: str, port: int, *, instance: str = "skep-queen", public_url: str | None = None) -> AsyncServiceHandle` — registers the service; handle has `async def close(self) -> None`.
  - `async def browse(timeout: float = 3.0) -> str | None` — returns a `ws://<addr>:<port>/ws` URL for the first discovered queen, or `None`.
  - `async def resolve_queen_url(config: WorkerConfig, *, browse_timeout: float = 3.0) -> str | None` — returns `config.queen_url` if set, else `browse(...)` when `config.use_mdns`, else `None`.

- [ ] **Step 1: Add `zeroconf` to `pyproject.toml`**

```toml
dependencies = ["aiogram>=3.13", "aiohttp>=3.10", "zeroconf>=0.130"]
```

Run: `uv sync`

- [ ] **Step 2: Write the failing test `tests/test_discovery.py`**

```python
import pytest

from skep import discovery
from skep.config import WorkerConfig
from pathlib import Path


def _wcfg(**kw):
    base = dict(
        host="g16", profile="work", claude_config_dir=None,
        repos_root=Path("/tmp"), worktrees_root=Path("/tmp"),
        db_path=":memory:",
    )
    base.update(kw)
    return WorkerConfig(**base)


async def test_resolve_prefers_explicit_url(monkeypatch):
    called = {"browsed": False}

    async def fake_browse(timeout=3.0):
        called["browsed"] = True
        return "ws://discovered:8765/ws"

    monkeypatch.setattr(discovery, "browse", fake_browse)
    cfg = _wcfg(queen_url="wss://skep.cyphy.kz/ws")
    assert await discovery.resolve_queen_url(cfg) == "wss://skep.cyphy.kz/ws"
    assert called["browsed"] is False  # no mDNS when URL is explicit


async def test_resolve_uses_mdns_when_no_url(monkeypatch):
    async def fake_browse(timeout=3.0):
        return "ws://discovered:8765/ws"

    monkeypatch.setattr(discovery, "browse", fake_browse)
    cfg = _wcfg(queen_url=None, use_mdns=True)
    assert await discovery.resolve_queen_url(cfg) == "ws://discovered:8765/ws"


async def test_resolve_returns_none_when_mdns_disabled_and_no_url():
    cfg = _wcfg(queen_url=None, use_mdns=False)
    assert await discovery.resolve_queen_url(cfg) is None


async def test_advertise_and_browse_roundtrip():
    handle = await discovery.advertise("127.0.0.1", 8765)
    try:
        url = await discovery.browse(timeout=3.0)
    finally:
        await handle.close()
    assert url is not None
    assert url.endswith(":8765/ws")
```

> The round-trip test needs a working multicast loopback; mark it with `@pytest.mark.mdns` and skip in CI if the environment blocks multicast. The first three tests (pure resolution logic) always run.

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_discovery.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'skep.discovery'`).

- [ ] **Step 4: Implement `src/skep/discovery.py`**

```python
from __future__ import annotations

import asyncio
import socket

from zeroconf import ServiceInfo, ServiceListener, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

from skep.config import WorkerConfig

SERVICE_TYPE = "_skep-queen._tcp.local."


class AsyncServiceHandle:
    def __init__(self, azc: AsyncZeroconf, info: ServiceInfo):
        self._azc = azc
        self._info = info

    async def close(self) -> None:
        await self._azc.async_unregister_service(self._info)
        await self._azc.async_close()


async def advertise(host: str, port: int, *, instance: str = "skep-queen",
                    public_url: str | None = None) -> AsyncServiceHandle:
    azc = AsyncZeroconf()
    properties: dict[bytes, bytes | None] = {b"host": instance.encode()}
    if public_url:
        properties[b"public_url"] = public_url.encode()
    info = ServiceInfo(
        SERVICE_TYPE,
        f"{instance}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(host)],
        port=port,
        properties=properties,
    )
    await azc.async_register_service(info)
    return AsyncServiceHandle(azc, info)


async def browse(timeout: float = 3.0) -> str | None:
    azc = AsyncZeroconf()
    found: asyncio.Queue[str] = asyncio.Queue()

    class _Listener(ServiceListener):
        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name, timeout=int(timeout * 1000))
            if info is None:
                return
            addrs = info.parsed_addresses()
            if addrs and info.port:
                found.put_nowait(f"ws://{addrs[0]}:{info.port}/ws")

        def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

        def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

    browser = AsyncServiceBrowser(azc.zeroconf, SERVICE_TYPE, _Listener())
    try:
        return await asyncio.wait_for(found.get(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        await browser.async_cancel()
        await azc.async_close()


async def resolve_queen_url(config: WorkerConfig, *,
                            browse_timeout: float = 3.0) -> str | None:
    if config.queen_url:
        return config.queen_url
    if config.use_mdns:
        return await browse(timeout=browse_timeout)
    return None
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_discovery.py -q -m "not mdns" && uvx pyright src`
Expected: PASS (resolution tests); pyright 0 errors. Run the round-trip locally with `uv run pytest tests/test_discovery.py -q` where multicast works.

- [ ] **Step 6: Register the `mdns` marker in `pyproject.toml`**

Under `[tool.pytest.ini_options]` add:

```toml
markers = ["mdns: requires working multicast loopback"]
```

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/skep/discovery.py tests/test_discovery.py
git commit -m "feat(discovery): mDNS advertise/browse + queen-url resolution"
```

---

## Task 9: Queen entrypoint (`skep-queen`)

Assembles the queen process: bot + dispatcher (reusing `build_dispatcher` from `app.py`) + `QueenWsServer` on an aiohttp site + mDNS advertise, all run concurrently. Adds the `skep-queen` console script.

**Files:**
- Create: `src/skep/queen/app.py`
- Modify: `pyproject.toml` (console script `skep-queen`)
- Test: `tests/test_queen_app.py`

**Interfaces:**
- Consumes: `load_queen_config`, `build_bot`, `Gateway`, `Bookkeeping`, `QueenSink`, `QueenRouter`, `build_dispatcher` (from `skep.app`), `QueenWsServer`, `discovery.advertise`.
- Produces:
  - `build_queen(qcfg: QueenConfig) -> tuple[Bot, Dispatcher, web.Application, QueenRouter]` — pure wiring, no I/O started (unit-testable).
  - `async def serve(qcfg: QueenConfig) -> None` — starts the aiohttp site, mDNS advertise (if `advertise_mdns`), and `dp.start_polling(bot)` concurrently.
  - `def run() -> None` — `asyncio.run(serve(load_queen_config(os.environ)))`.

- [ ] **Step 1: Write the failing test `tests/test_queen_app.py`**

```python
from skep.config import QueenConfig
from skep.queen.app import build_queen


def _qcfg():
    return QueenConfig(bot_token="123:abc", owner_id=42, group_chat_id=-100,
                       shared_secret="s", bookkeeping_db=":memory:")


def test_build_queen_wires_ws_route():
    bot, dp, app, router = build_queen(_qcfg())
    paths = [r.resource.canonical for r in app.router.routes()
             if r.resource is not None]
    assert "/ws" in paths
    assert router is not None
    # dispatcher has the owner-gated commands registered
    assert dp is not None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_queen_app.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'skep.queen.app'`).

- [ ] **Step 3: Implement `src/skep/queen/app.py`**

```python
from __future__ import annotations

import asyncio
import os

from aiogram import Bot, Dispatcher
from aiohttp import web

from skep.app import build_dispatcher
from skep.config import QueenConfig, load_queen_config
from skep.discovery import advertise
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.router import QueenRouter
from skep.queen.telegram_sink import QueenSink
from skep.telegram_gw import Gateway, build_bot
from skep.ws_transport import QueenWsServer


def build_queen(qcfg: QueenConfig) -> tuple[Bot, Dispatcher, web.Application, QueenRouter]:
    bot = build_bot(qcfg)
    gateway = Gateway(bot, qcfg)
    bk = Bookkeeping.open(qcfg.bookkeeping_db)
    sink = QueenSink(gateway, bk)
    router = QueenRouter(bk)
    app = web.Application()
    QueenWsServer(router, sink, qcfg.shared_secret).attach(app)
    dp = build_dispatcher(router, qcfg)
    return bot, dp, app, router


async def serve(qcfg: QueenConfig) -> None:
    bot, dp, app, _router = build_queen(qcfg)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, qcfg.listen_host, qcfg.listen_port)
    await site.start()

    handle = None
    if qcfg.advertise_mdns:
        # advertise on the loopback/LAN address; a real deploy sets listen_host
        adv_host = "127.0.0.1" if qcfg.listen_host in ("0.0.0.0", "") else qcfg.listen_host
        handle = await advertise(adv_host, qcfg.listen_port,
                                 public_url=qcfg.public_url)
    try:
        await dp.start_polling(bot)
    finally:
        if handle is not None:
            await handle.close()
        await runner.cleanup()


def run() -> None:
    asyncio.run(serve(load_queen_config(os.environ)))
```

- [ ] **Step 4: Add the console script to `pyproject.toml`**

```toml
[project.scripts]
skep = "skep.app:run"
skep-queen = "skep.queen.app:run"
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_queen_app.py -q && uvx pyright src`
Expected: PASS; pyright 0 errors.

- [ ] **Step 6: Commit**

```bash
git add src/skep/queen/app.py pyproject.toml tests/test_queen_app.py
git commit -m "feat(queen): skep-queen entrypoint (WS server + bot + mDNS)"
```

---

## Task 10: Worker entrypoint (`skepd`)

Assembles the worker process: `Registry` + `SwitchableEventSink` + `Supervisor` + `WorkerWsClient`, resolving the queen URL via `discovery.resolve_queen_url`, running the reconnect loop. Adds the `skepd` console script.

**Files:**
- Create: `src/skep/worker/__init__.py`, `src/skep/worker/app.py`
- Modify: `pyproject.toml` (console script `skepd`)
- Test: `tests/test_worker_app.py`

**Interfaces:**
- Consumes: `load_worker_config`, `Registry`, `SwitchableEventSink`, `Supervisor`, `WorkerWsClient`, `discovery.resolve_queen_url`.
- Produces:
  - `def build_worker(wcfg: WorkerConfig) -> tuple[Supervisor, SwitchableEventSink, WorkerWsClient]` — pure wiring.
  - `async def serve(wcfg: WorkerConfig) -> None` — resolves the queen URL (mDNS or `queen_url`), sets it on the client's config, and runs the reconnect loop; raises `SystemExit` with a clear message if no URL can be resolved.
  - `def run() -> None`.

- [ ] **Step 1: Write the failing test `tests/test_worker_app.py`**

```python
from pathlib import Path

from skep.config import WorkerConfig
from skep.worker.app import build_worker
from skep.transport import SwitchableEventSink
from skep.supervisor import Supervisor


def _wcfg(**kw):
    base = dict(
        host="g16", profile="work", claude_config_dir=None,
        repos_root=Path("/tmp"), worktrees_root=Path("/tmp"),
        db_path=":memory:", shared_secret="s",
    )
    base.update(kw)
    return WorkerConfig(**base)


def test_build_worker_wires_supervisor_to_switch():
    sup, switch, client = build_worker(_wcfg())
    assert isinstance(sup, Supervisor)
    assert isinstance(switch, SwitchableEventSink)
    # the supervisor's sink IS the switch, so reconnects can swap the target
    assert sup._sink is switch  # type: ignore[attr-defined]
    assert client is not None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_worker_app.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'skep.worker'`).

- [ ] **Step 3: Implement the worker package**

`src/skep/worker/__init__.py`:

```python
```

(empty file)

`src/skep/worker/app.py`:

```python
from __future__ import annotations

import asyncio
import os
import sys

from skep.config import WorkerConfig, load_worker_config
from skep.db import Registry
from skep.discovery import resolve_queen_url
from skep.supervisor import Supervisor
from skep.transport import SwitchableEventSink
from skep.ws_transport import WorkerWsClient


def build_worker(wcfg: WorkerConfig) -> tuple[Supervisor, SwitchableEventSink, WorkerWsClient]:
    registry = Registry.open(wcfg.db_path)
    switch = SwitchableEventSink()
    supervisor = Supervisor(wcfg, registry, switch)
    client = WorkerWsClient(wcfg, supervisor, switch, wcfg.shared_secret)
    return supervisor, switch, client


async def serve(wcfg: WorkerConfig) -> None:
    url = await resolve_queen_url(wcfg)
    if url is None:
        raise SystemExit(
            "no queen: set SKEP_QUEEN_URL or enable mDNS (SKEP_USE_MDNS=1)")
    # freeze the resolved URL onto the config the client reads
    wcfg = replace_queen_url(wcfg, url)
    _sup, _switch, client = build_worker(wcfg)
    await client.run()


def replace_queen_url(wcfg: WorkerConfig, url: str) -> WorkerConfig:
    import dataclasses
    return dataclasses.replace(wcfg, queen_url=url)


def run() -> None:
    try:
        asyncio.run(serve(load_worker_config(os.environ)))
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        raise
```

> `Supervisor.__init__` binds `self._sink` to the passed sink; passing the `SwitchableEventSink` is what lets `WorkerWsClient` swap the live target on each (re)connection without recreating the Supervisor.

- [ ] **Step 4: Add the console script to `pyproject.toml`**

```toml
[project.scripts]
skep = "skep.app:run"
skep-queen = "skep.queen.app:run"
skepd = "skep.worker.app:run"
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_worker_app.py -q && uvx pyright src`
Expected: PASS; pyright 0 errors.

- [ ] **Step 6: Commit**

```bash
git add src/skep/worker/ pyproject.toml tests/test_worker_app.py
git commit -m "feat(worker): skepd entrypoint (Supervisor + WS client + discovery)"
```

---

## Task 11: Queen group auto-onboarding (§10.1)

Lets the queen self-onboard to a control group instead of a hardcoded `group_chat_id`: learn a group via `my_chat_member`, gate on owner membership, register per-chat commands, check forum readiness, and post a setup prompt if something is missing. Makes `group_chat_id` optional.

**Files:**
- Create: `src/skep/queen/onboarding.py`
- Modify: `src/skep/queen/app.py` (register the `my_chat_member` handler)
- Test: `tests/test_onboarding.py`

**Interfaces:**
- Consumes: `aiogram` types (`ChatMemberUpdated`), `QueenConfig.owner_id`.
- Produces:
  - `async def is_owner_member(bot, chat_id: int, owner_id: int) -> bool` — `getChatMember(chat_id, owner_id)` returns a non-`left`/`kicked` status.
  - `async def onboard_group(bot, chat_id: int, owner_id: int) -> str` — returns one of `"skipped"` (owner not present), `"needs_forum"` (owner present but chat is not a forum / bot lacks `can_manage_topics`), or `"ready"` (registers per-chat commands and returns ready). Posts a plain setup prompt to the chat on `needs_forum`.

- [ ] **Step 1: Write the failing test `tests/test_onboarding.py`**

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from skep.queen.onboarding import onboard_group


def _bot(owner_status="member", is_forum=True, can_manage_topics=True):
    bot = MagicMock()
    owner_member = MagicMock(status=owner_status)
    bot_member = MagicMock(status="administrator",
                           can_manage_topics=can_manage_topics)

    async def get_chat_member(chat_id, user_id):
        return owner_member if user_id == 42 else bot_member

    chat = MagicMock(is_forum=is_forum)
    bot.get_chat_member = AsyncMock(side_effect=get_chat_member)
    bot.get_chat = AsyncMock(return_value=chat)
    bot.get_me = AsyncMock(return_value=MagicMock(id=999))
    bot.set_my_commands = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


async def test_onboard_skips_when_owner_absent():
    bot = _bot(owner_status="left")
    assert await onboard_group(bot, chat_id=-100, owner_id=42) == "skipped"
    bot.set_my_commands.assert_not_called()


async def test_onboard_prompts_when_not_forum():
    bot = _bot(is_forum=False)
    assert await onboard_group(bot, chat_id=-100, owner_id=42) == "needs_forum"
    bot.send_message.assert_awaited()  # setup prompt posted


async def test_onboard_ready_registers_commands():
    bot = _bot()
    assert await onboard_group(bot, chat_id=-100, owner_id=42) == "ready"
    bot.set_my_commands.assert_awaited()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_onboarding.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'skep.queen.onboarding'`).

- [ ] **Step 3: Implement `src/skep/queen/onboarding.py`**

```python
from __future__ import annotations

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat

_COMMANDS = [
    BotCommand(command="spawn", description="Spawn an agent: <host> [--profile p] <repo> <task>"),
    BotCommand(command="ls", description="List active agents"),
    BotCommand(command="kill", description="Kill an agent by ref"),
    BotCommand(command="panic", description="Kill all agents"),
]

_SETUP_PROMPT = (
    "skep queen is here, but this group isn't ready. Enable Topics "
    "(group settings → Topics) and grant me admin with 'Manage Topics', "
    "then re-add me or re-check."
)


async def is_owner_member(bot: Bot, chat_id: int, owner_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, owner_id)
    return member.status not in ("left", "kicked")


async def onboard_group(bot: Bot, chat_id: int, owner_id: int) -> str:
    if not await is_owner_member(bot, chat_id, owner_id):
        return "skipped"
    chat = await bot.get_chat(chat_id)
    me = await bot.get_me()
    my_member = await bot.get_chat_member(chat_id, me.id)
    can_manage = getattr(my_member, "can_manage_topics", False)
    if not getattr(chat, "is_forum", False) or not can_manage:
        await bot.send_message(chat_id, _SETUP_PROMPT)
        return "needs_forum"
    await bot.set_my_commands(_COMMANDS, scope=BotCommandScopeChat(chat_id=chat_id))
    return "ready"
```

- [ ] **Step 4: Register the handler in `src/skep/queen/app.py`**

In `build_queen`, after building `dp`, wire a `my_chat_member` handler (import `ChatMemberUpdated` from `aiogram.types` and `onboard_group`):

```python
    from aiogram.types import ChatMemberUpdated
    from skep.queen.onboarding import onboard_group

    @dp.my_chat_member()
    async def _on_added(event: ChatMemberUpdated) -> None:
        await onboard_group(bot, event.chat.id, qcfg.owner_id)
```

> `group_chat_id` stays in `QueenConfig` for Phase-1 compatibility but is no longer required to be preconfigured — onboarding learns the chat id at runtime. Leave the field; do not remove it.

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_onboarding.py tests/test_queen_app.py -q && uvx pyright src`
Expected: PASS; pyright 0 errors.

- [ ] **Step 6: Commit**

```bash
git add src/skep/queen/onboarding.py src/skep/queen/app.py tests/test_onboarding.py
git commit -m "feat(queen): group auto-onboarding via my_chat_member"
```

---

## Task 12: Two-worker end-to-end + auth-reject capstone

The integration test the design's §14 calls for: a real WS queen with **two** workers on different profiles, spawning on each and aggregating in `/ls`, plus an end-to-end auth-reject. Uses the real `Supervisor` + `fake_claude` stub over the WebSocket.

**Files:**
- Modify: `tests/test_integration.py`
- Test: itself

**Interfaces:**
- Consumes: `QueenWsServer`, `WorkerWsClient`, `build_worker` (Task 10), `fake_claude_cmd`/`git_repo` fixtures, `QueenSink`, `QueenRouter`, `Bookkeeping`.

- [ ] **Step 1: Add the failing e2e tests to `tests/test_integration.py`**

```python
from aiohttp import web
from aiohttp.test_utils import TestServer
import aiohttp

from skep.queen.router import QueenRouter
from skep.queen.telegram_sink import QueenSink
from skep.ws_transport import QueenWsServer, WorkerWsClient
from skep.transport import SwitchableEventSink
from skep.supervisor import Supervisor
from skep.db import Registry


async def _start_queen(secret="s"):
    gw = _gateway()
    bk = Bookkeeping.open(":memory:")
    router = QueenRouter(bk)
    sink = QueenSink(gw, bk)
    app = web.Application()
    QueenWsServer(router, sink, secret).attach(app)
    server = TestServer(app)
    await server.start_server()
    url = f"ws://127.0.0.1:{server.port}/ws"
    return server, url, router, bk, gw


def _worker(wcfg, url, secret="s"):
    registry = Registry.open(":memory:")
    switch = SwitchableEventSink()
    sup = Supervisor(wcfg, registry, switch)
    client = WorkerWsClient(wcfg, sup, switch, secret)
    return sup, client


async def test_two_worker_spawn_and_ls(tmp_path, git_repo, fake_claude_cmd):
    repo_name = git_repo.name
    server, url, router, bk, gw = await _start_queen()

    def wcfg(profile):
        return WorkerConfig(
            host="g16", profile=profile, claude_config_dir=None,
            repos_root=git_repo.parent, worktrees_root=tmp_path / f"wt-{profile}",
            db_path=":memory:", queen_url=url, shared_secret="s",
            claude_bin=fake_claude_cmd,
        )

    _sup_w, client_w = _worker(wcfg("work"), url)
    _sup_p, client_p = _worker(wcfg("personal"), url)
    try:
        async with aiohttp.ClientSession() as s1, aiohttp.ClientSession() as s2:
            t1 = asyncio.create_task(client_w.run_once(s1, url))
            t2 = asyncio.create_task(client_p.run_once(s2, url))

            async def spawn_when_ready(profile):
                for _ in range(200):
                    try:
                        await router.cmd_spawn("g16", profile, repo_name, "clean")
                        return
                    except Exception:
                        await asyncio.sleep(0.02)

            await spawn_when_ready("work")
            await spawn_when_ready("personal")

            for _ in range(300):
                actives = bk.list_active()
                if len(actives) >= 2:
                    break
                await asyncio.sleep(0.02)
            t1.cancel()
            t2.cancel()
    finally:
        await server.close()

    ls = router.format_ls()
    assert "work" in ls
    assert "personal" in ls


async def test_wrong_secret_worker_never_registers():
    server, url, router, bk, gw = await _start_queen(secret="right")
    from pathlib import Path
    wcfg = WorkerConfig(
        host="g16", profile="work", claude_config_dir=None,
        repos_root=Path("/tmp"), worktrees_root=Path("/tmp"),
        db_path=":memory:", queen_url=url, shared_secret="wrong",
    )
    _sup, client = _worker(wcfg, url, secret="wrong")
    try:
        async with aiohttp.ClientSession() as sess:
            task = asyncio.create_task(client.run_once(sess, url))
            await asyncio.sleep(0.3)
            task.cancel()
            # command to the never-registered worker must fail
            with pytest.raises(Exception):
                await router.cmd_spawn("g16", "work", "nix", "task")
    finally:
        await server.close()
```

- [ ] **Step 2: Run to verify they fail (or pass) meaningfully**

Run: `uv run pytest tests/test_integration.py -q`
Expected: the two new tests are collected; they should PASS once the prior tasks are in. If `test_two_worker_spawn_and_ls` flakes on timing, raise the poll counts — do not add real sleeps to production code.

- [ ] **Step 3: Full suite + pyright**

Run: `uv run pytest -q && uvx pyright src`
Expected: all tests PASS (Plan-1's 69 + the Plan-2 additions); pyright 0 errors.

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration.py
git commit -m "test(ws): two-worker WS e2e + auth-reject integration"
```

---

## Self-Review notes (for the executor)

- **Spec coverage** — §5 seam (unchanged + `on_spawn_rejected`): Tasks 4–5. §6.1 discovery: Task 8. §6.2 reconnect/re-attach: Task 7. §6.3 capacity→`spawn_rejected`: Task 5. §6.4 heartbeat/presence + invariant: Task 6. §7 wire protocol: Task 2. §8 identity/routing (`ref`, detached `/ls`): Tasks 4/6. §9 mutual auth: Task 3 + enforced in 4/5/12. §10 deploy contract (config only): Task 1. §10.1 onboarding: Task 11. §14 testing (two-worker e2e, auth reject, mDNS round-trip, profile isolation carried from Plan 1): Tasks 8/12. §15 file list: matches the File Structure above.
- **Out of this plan (by design):** the actual VPS/Caddy wiring (lives in `~/gh/vps`); Phase-3 talk-back frames (`ask_human` etc.); Phase-4 sandbox/resume; the L0-mailbox MCP-shim spike (next after this plan).
- **Deviation from §15:** `telegram_gw.py`/`formatting.py` stay at `src/skep/` (not moved into `queen/`) to avoid the type-coupling regression noted in project memory; they are queen-only imports already.
- **Timing in tests:** all cross-task async tests poll with bounded loops + short `asyncio.sleep`, never fixed sleeps in production code. If CI is slow, raise poll counts.
```