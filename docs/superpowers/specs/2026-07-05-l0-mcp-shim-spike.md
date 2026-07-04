# L0 MCP-Shim Spike — Resolution

**Date:** 2026-07-05
**Status:** Resolved — unblocks the L0 Mailbox build (`2026-07-05-l0-mailbox-design.md`).
**Resolves:** the four open questions in L0 spec §15, reconciled against the
current `src/skep/` layout (post Phase-2 Plan 2, merge `78f872e`).

Verified against `claude` **2.1.201** and the merged transport seam
(`transport.py`, `ws_transport.py`, `supervisor.py`, `agent.py`).

---

## 0. Decisions at a glance

| Question (§15) | Decision |
|---|---|
| Shim transport type | **Streamable-HTTP MCP server, hosted *in the worker process*** on `127.0.0.1` — not stdio. |
| Server topology | **One aiohttp server per worker**, multiplexed by a **per-agent bearer token**. Not one-server-per-agent-port. |
| Agent → shim injection | `--mcp-config '<inline JSON>'` at spawn (server url + `Authorization: Bearer <token>`). **No `--strict-mcp-config`** (agent keeps its profile's own MCP, e.g. gortex). |
| Agent → id binding | Bind to the **worker-local `tid`** (known synchronously at spawn), **not** the queen `ref`. `from` is resolved to `ref` at the queen via existing bookkeeping. |
| Shim → queen forwarding | **Extend the seam with request/reply correlation** (`req_id` + a `dict[req_id, Future]` on the worker WS client). Mailbox tools are request/reply; today's seam is fire-and-forget. Persist-before-ack. |
| Rate / depth / dedupe defaults | 20 msg/min per sender, reply-chain depth cap 10, dedupe window 60s. Injected clock; tune with real traffic. |
| `read_inbox` polling cadence | Pure-pull. System-prompt guidance + a "you have N unread" nudge appended to the agent's activity context. No push in L0. |
| Body cap | 16 KB reject. Large payloads become L1-memory references once L1 exists. |

---

## 1. Q1 — MCP shim transport binding (the load-bearing one)

### 1.1 Transport type: in-process HTTP, not stdio

The shim must forward tool calls over the **existing in-process seam** —
`SwitchableEventSink` / `WorkerWsClient`, which live in the worker process and
share its asyncio loop.

- **stdio (rejected):** `claude` spawns a stdio MCP server as a **child of the
  agent** — a separate process with no handle to the worker's seam. It would need
  a *second* IPC hop back to the worker (another socket), doubling the plumbing
  and the lifecycle management, per agent.
- **HTTP streamable (chosen):** a single aiohttp app hosted **inside the worker
  process** on `127.0.0.1:<port>`. Its MCP tool handlers call the seam **directly**
  (in-proc), exactly the spec-§3 diagram: `agent → localhost → shim (in worker) →
  transport seam → queen`. The worker already depends on `aiohttp` (WS), so no new
  dep. SSE is legacy MCP transport; use **streamable-http**.

### 1.2 Topology: one server per worker, per-agent bearer token

Not one port per agent (socket churn, port exhaustion, per-agent server
lifecycle). Instead: **one localhost aiohttp server**, and each agent authenticates
with a **unique unguessable bearer token** minted at spawn. The worker holds
`token → tid`. Every tool call resolves its token to exactly one `tid`.

This structurally enforces L0 spec §11 (*`from` is queen-assigned, not
client-supplied*): the agent cannot name its own identity — the shim derives it
from the token's bound `tid`. Two guard layers: **localhost-only bind** (never
`0.0.0.0` — matches the project-memory rule that the worker's only inbound server
is localhost + agent-bound) **+ token** (defends against other local processes
guessing the port).

### 1.3 Injection into the agent (reconcile with Phase-1 spawn)

`claude --mcp-config` accepts **inline JSON strings** (not just files) and an
`http` transport with headers — both confirmed in 2.1.201. So no per-agent temp
file: `AgentProcess._argv()` appends

```
--mcp-config {"mcpServers":{"skep":{"type":"http",
  "url":"http://127.0.0.1:<port>/mcp",
  "headers":{"Authorization":"Bearer <per-agent-token>"}}}}
```

**Omit `--strict-mcp-config`** — that flag makes `--mcp-config` *exclusive* and
would strip the agent's profile MCP servers (gortex etc.). We only want to *add*
the shim.

`AgentProcess.__init__` gains `mcp_url: str | None` + `mcp_token: str | None`;
`Supervisor.spawn` mints the token, registers `token → tid`, and passes both. The
existing `CLAUDE_CONFIG_DIR` / clean-env path is unchanged. Everything else in
the Phase-1 spawn path (worktree, DEVNULL stdin, stream-json) stays.

### 1.4 Binding id: worker-local `tid`, not the queen `ref`

`Registry.add_task` returns the worker-local `tid` **synchronously** at spawn;
the queen `ref` is assigned **asynchronously** and the current
`EventSink.task_started` is fire-and-forget (`-> None`). Binding to `ref` would
force a queen round-trip before the agent could start.

So: the shim stamps the worker-local `tid`; mailbox frames carry
`(host, profile, local_id=tid)`; the **queen** resolves `from` via the *same*
`(host, profile, local_id) → ref` bookkeeping map it already maintains for
`/ls` and `/kill`. Reuses existing identity plumbing; keeps spawn non-blocking.
The agent never sees any id — it just calls `send_message(to, …)`.

### 1.5 Forwarding: the seam needs request/reply (this is the real new machinery)

Today's seam is **fire-and-forget in each direction**. Mailbox tools are
**request/reply** — `send_message` must return an ack only *after* the queen
persists the row (persist-before-ack), and `read_inbox` must return rows. That
correlated response has no home in the current wire.

**Add a thin correlation layer** on the WS:

- New wire frames (extend `wire.py`): worker→queen `mailbox_send {req_id,
  local_id, to, subject, body}` and `inbox_read {req_id, local_id}`; queen→worker
  `mailbox_ack {req_id, ok, error?}` and `inbox_reply {req_id, messages:[…]}`.
- Worker side: a new `MailboxClient` holds `dict[req_id, asyncio.Future]`, sends
  the request frame over the same `ClientWebSocketResponse`, and awaits the
  Future. `WorkerWsClient.run_once`'s receive loop routes `mailbox_ack` /
  `inbox_reply` frames to their Future (alongside its existing command handling).
- Queen side: `QueenWsServer._dispatch` gains `mailbox_send` / `inbox_read`
  branches → `queen/mailbox.py` (persist / read) → reply frame with the same
  `req_id`. The ack is sent **after** the SQLite commit.
- **L1 memory reuses this exact layer** (`memory_write`/`memory_search` become two
  more request/reply frames + two more shim tools). Build the correlation layer
  once, generically.

### 1.6 Detached-worker behavior (do not hang the agent)

When the queen link is down, `SwitchableEventSink.target is None` and events are
dropped silently — fine for fire-and-forget. But a mailbox tool is **blocking on a
reply**. So: if the link is down (or the Future times out), the shim returns a
**structured, retryable error** to the agent (`"fleet link unavailable, retry"`),
never hangs. This composes with persist-before-ack at-least-once: nothing is
acked ⇒ the agent retries ⇒ dedupe catches any eventual duplicate.

---

## 2. Q2 — rate-limit / depth-cap / dedupe defaults

Starting points (L0 spec §8), all driven by an **injected clock** so tests are
deterministic:

- **Rate limit:** 20 messages / 60s per sender `ref`. (N+1)th in-window rejected
  with a retryable error; sustained breach alerts the CEO.
- **Reply-chain depth cap:** 10 hops. `hops > cap` → drop + dead-letter + notify.
- **Content dedupe window:** 60s on `(from, to, hash(subject+body))`.

These are config knobs, not constants — tune once real fleet traffic exists.

---

## 3. Q3 — `read_inbox` polling cadence

L0 is **pure-pull** by contract (no push into a running agent — that's P3
soft-steer, blocked on stdin stream-json). Ergonomics without a push channel:

- **System-prompt guidance** injected at spawn: "you have a mailbox; call
  `read_inbox` at natural breakpoints (after finishing a sub-task, before
  reporting done)."
- **Passive nudge:** when the queen holds unread rows for an agent, piggyback a
  `"(N unread messages)"` marker onto the next thing the agent already sees. No
  new channel, no interrupt.

No timer-based self-reminder in L0 — revisit if agents demonstrably ignore their
inbox.

---

## 4. Q4 — body cap

**16 KB**, rejected over cap with a clear error. Once **L1 memory** lands, large
payloads should be written to memory and the message carries a **reference**
(id) instead of the blob — keeps the mailbox row-store small and the WS frames
bounded.

---

## 5. Testing path (reconciled with the current fixtures)

- **`fake_claude` cannot invoke MCP tools** — it emits canned stream-json and
  ignores flags. So a *live* MCP tool round-trip is an **integration/manual smoke
  test** against a real `claude`, not a unit test.
- **Unit-test the shim tool functions directly** (call the `send_message` /
  `read_inbox` handlers with a bound `tid`, no HTTP, no `claude`).
- **Unit-test the request/reply seam** with an in-memory bidirectional fake
  (extends the existing `InMemoryEventSink` → `QueenInbox` pattern with a reply
  path) + injected clock.
- **Mailbox store, addressing, dead-letter, loop-prevention** — as L0 spec §14,
  all over the in-memory seam.
- One **integration test**: real `claude` spawned with the injected
  `--mcp-config`, asserting a `send_message` call lands a persisted row (guards
  the injection + HTTP + token-binding wiring the unit tests stub out).

---

## 6. Impact on the L0 spec

- **§13 files:** `worker/mcp_shim.py` = in-process aiohttp streamable-HTTP MCP
  server + `token → tid` map. Add `worker/mailbox_client.py` (the `req_id`/Future
  correlation client). `queen/mailbox.py` unchanged in intent.
- **New wire frames:** `mailbox_send` / `mailbox_ack` / `inbox_read` /
  `inbox_reply` (§1.5) — the generic request/reply layer L1 reuses.
- **`agent.py` / `supervisor.py`:** `AgentProcess` gains `mcp_url` + `mcp_token`;
  `Supervisor.spawn` mints the token and registers the binding.
- **No new network/trust surface:** the shim is localhost-only, token-bound to one
  agent; forwarding rides the existing mutually-authenticated WS.

---

## 7. Follow-ups (non-blocking)

- The request/reply correlation layer wants a **timeout + Future-cleanup** on
  worker reconnect (drop pending Futures with a retryable error, mirroring the
  Plan-2 reconnect-clobber guard).
- Confirm streamable-HTTP (not SSE) is the transport `claude` 2.1.201 negotiates
  for an `http` server — settle at first integration test.
