# skep — architecture and concepts

**Describes `metheoryt/ubuntu26-skep-A2-resume` at `73400a4` (2026-07-24, branched
from `main`@`e6213a2`), read on 2026-07-24.**
Since the previous stamp (`3dfda20`, 2026-07-23, now merged as `main`@`e6213a2`)
**Sessions A3** landed on this not-yet-merged branch: usage-limit detection, the
`parked` state, `/resume`, and the queen's auto-resume sweep (§3 has the loop, §7
the status). This file is written by hand and does not regenerate. When it disagrees
with the code, the code is right — fix this file. Overwrite it in place; never add
a dated copy.

---

## 1. What skep is

skep runs headless Claude Code agents on your machines, and you drive them from
Telegram. You send `/spawn <host> <profile> <repo> <task>`; a Claude Code process
starts in a fresh git worktree; its output streams back into a Telegram topic
dedicated to that task.

Everything else in this document exists to serve that sentence, or to serve a
longer-term goal (§7) that sentence is the first step toward.

---

## 2. The three processes

There are three console scripts. Two of them are the real deployment; the third is
a convenience.

| Script | Entrypoint | Role |
|---|---|---|
| `skep-queen` | `skep.queen.app:run` | Owns the Telegram bot. WebSocket **server**. One per fleet. |
| `skepd` | `skep.worker.app:run` | Spawns agents. WebSocket **client**, dials the queen. One per (host, profile). |
| `skep` | `skep.app:run` | Queen + one worker in a single process over an in-memory transport. |

In each module `run()` is a thin sync wrapper the console script points at; the
async body is `serve()` (queen, worker) or `main()` (combined).

`skep` is not a legacy stopgap — it is a supported single-host mode that skips the
network entirely. Both shapes are live.

Two further process kinds exist at runtime but have no console script:

- **The agent** — a `claude` subprocess spawned by the worker, one per task, with
  `cwd` set to a fresh git worktree.
- **The memory shim** — a stdio MCP server (`python -m skep.worker.memory_shim`)
  spawned as a child *of `claude`*, not of skep. It exposes one tool, `remember`.

There is also a **mailbox shim**: an HTTP MCP server the worker runs in-process,
one per agent on an ephemeral port, exposing `send_message` and `read_inbox`.

So a fully distributed fleet is: one `skep-queen`, N `skepd`, each `skepd` holding
M `claude` children, each `claude` holding one stdio shim child.

> There is **no separate Telegram gateway process.** `telegram_gw.py` is a module
> inside the queen.

---

## 3. The path of a message

Inbound and outbound are different routes. They are not the reverse of each other.

**Inbound — your Telegram message becomes a running agent:**

1. `aiogram`'s `Bot` (built in `telegram_gw.build_bot`) long-polls Telegram.
2. The ownership gate rejects everyone but you: an outer middleware plus a filter,
   both calling `telegram_gw.is_owner`. **This, not `auth.py`, is Telegram authorization.**
3. `queen.router.QueenRouter.cmd_spawn` looks up the handler registered for
   `(host, profile)`.
4. That handler is either a `Supervisor` directly (single-process mode) or a
   `ws_transport.RemoteWorker` (distributed), which encodes `wire.spawn_msg` and
   sends it over that worker's socket.
5. Worker side: `ws_transport.WorkerWsClient` decodes the frame and calls
   `Supervisor.spawn`.
6. `Supervisor.spawn` — now a thin wrapper over `spawn_workspace` — enforces
   `max_concurrent`, creates the worktree, records a row in the worker's `Registry`
   (stamping `session_local_id = its own tid` for a first invocation), assembles the
   MCP server map, **writes it to a `0600 <worktree>/.skep/mcp.json`** so the bearer
   token never rides argv, builds the tool grant, and starts an `AgentProcess`.

**Outbound — agent output becomes Telegram messages:**

7. `AgentProcess.events()` reads the `claude` process's stdout. Each line goes
   through `stream.parse_event`, a pure function yielding an `Event`
   (`system` / `assistant_text` / `tool_use` / `tool_result` / `result`).
8. `Supervisor.run_events` pumps those events into an `EventSink` —
   `InMemoryEventSink` or `WsEventSink`.
9. Distributed: the queen's `QueenWsServer` receives the frames and calls its
   `QueenInbox`. Both sink implementations converge on the same place.
10. `queen.telegram_sink.QueenSink` turns events into Telegram operations via
    `Gateway` (create topic, post, edit), recording message IDs in `Bookkeeping`.

**Terminal states.** `run_events` ends every invocation in exactly one of
`done` / `failed` / `killed` / **`parked`**. The first three are final. `parked`
is not: the session is *live but idle* — its worktree, its `ref` and its Telegram
topic all persist, only the `claude` process is gone. Anything that treats "a
terminal arrived" as "the session is finished" must special-case it; `QueenSink`
does (it skips the mailbox `handle_recipient_gone` teardown on a park), and
`Bookkeeping._ACTIVE` counts `parked` as active so `/ls` still lists it.

**The park → sweep → resume loop (Sessions A3):**

11. `stream.detect_usage_limit(ev)` inspects each `result` event and returns a
    `UsageLimit(reset_at)` when the runner reports an account usage limit.
    `run_events` then sets `terminal = "parked"` instead of `failed`, and carries
    the reset through `EventSink.done(..., reset_at=)` — which the `done` wire
    frame also carries.
12. `QueenSink.on_done` parks: `parked_until` is `reset_at` (or, when the runner
    gave none, `now + SKEP_PARK_DEFAULT_BACKOFF`, 1 h) plus 0–60 s of jitter,
    written by `Bookkeeping.park(ref, until)`.
    The topic gets one `⏸ parked (usage limit) · resumes ~HH:MM` post. The jitter
    stops a fleet that parked together from resuming in lockstep onto the same
    limit.
13. `queen/assembly.py:_park_sweep_loop` is the scheduler: every
    `SKEP_PARK_SWEEP_INTERVAL` (30 s) it walks `Bookkeeping.parked_due(now)`,
    skips workers that are not `router.is_online`, and calls
    `QueenRouter.cmd_resume(ref, origin="sweep")`. `now` is wall-clock, never
    monotonic — `parked_until` is a POSIX timestamp in the journal, so a queen
    restart must still see the same deadline. Every edge falls out of
    re-evaluation: offline worker, full worker, queen restart, an early reset
    guess (the agent re-hits the limit and re-parks) are all just "retried next
    tick". The loop runs in **both** runtime shapes — `build_queen` installs it on
    `app.cleanup_ctx`; single-process `skep.app:main` runs it as a bare task
    alongside the CEO-retry loop.
14. `cmd_resume` is the same shape as `cmd_spawn`: look up `(host, profile)`, then
    either a local `Supervisor` or a `RemoteWorker` that encodes
    `wire.resume_msg(session_local_id, model, origin)` and sends it. The worker's
    `WorkerWsClient` decodes it into `Supervisor.resume`.
15. `Supervisor.resume` opens a **new invocation of the same session**: same
    worktree, `claude --resume <resume_token>`, and `task_started(...,
    session_local_id)` back to the queen — where `QueenSink.on_task_started` finds
    the session via `by_session` and calls `rebind_invocation`, which repoints the
    **same ref and the same topic** at the new invocation and clears
    `parked_until`. Output resumes in the topic where it parked.

`/resume <ref> [--model <m>]` drives steps 14–15 by hand, as an early-unpark
override. It answers optimistically ("Resuming ref N") because the dispatch is
fire-and-forget on the split path.

**Two things about that loop are easy to get wrong.**

*Where deduplication lives.* `cmd_resume`'s `status != 'running'` check is a cheap
filter, **not** mutual exclusion — it never writes status, and `running` is set
only when the worker's `task_started` round-trips back into `rebind_invocation`,
a window as wide as a process spawn. A human's `/resume` racing the sweep is real.
The actual guard is `Supervisor.resume`'s `_live_sessions: set[int]` claim, taken
without an intervening `await` and released in both `resume`'s failure branch and
`run_events`' `finally`; a duplicate resume raises `ValueError`.

*How a rejection comes back, by shape.* Single-process: the handler **is** a local
`Supervisor`, so `CapacityError`/`ValueError` raise synchronously into the sweep's
own `except`. Split-queen: `RemoteWorker.resume` is fire-and-forget (sends a frame,
returns `0`, can never raise), so the rejection arrives later and out of band as a
`spawn_rejected` frame carrying `action="resume"` and the echoed `origin`.
`QueenSink.on_spawn_rejected` **logs instead of posting when `origin == "sweep"`** —
nothing on the rejection path clears `parked`, so a worker that stays full would
otherwise page the owner once every sweep tick, forever.

The agent is spawned with roughly:

```
claude -p <task> --output-format stream-json --verbose
       [--append-system-prompt <memory addendum>]
       [--mcp-config <worktree>/.skep/mcp.json]   # a PATH, not inline JSON
       [--allowedTools Bash,Edit,Write,mcp__memory__remember,...]
       [--add-dir <path> ...] [--model <id>] [--resume <token>]
```

`stdin` is `/dev/null`. `--input-format stream-json` is deliberately not used; it
blocks on stdin until EOF. `--mcp-config` takes a **file path** (written by
`worker/mcp_config.py:write_mcp_config`), not the inline JSON the earlier build
passed — that moved the per-agent bearer token off `/proc/<pid>/cmdline` (L0.2).
`--add-dir`/`--model`/`--resume` are the Sessions A1 argv: they render only when a
multi-root workspace, a model pin, or a resume token is present, so a plain first
spawn looks exactly as before.

---

## 4. The seams

Three files are easy to confuse. They do different jobs.

- **`transport.py`** — the abstract protocols, and their in-memory implementations.
  No I/O, no JSON. The protocols are `EventSink` (worker→queen), `CommandHandler`
  (queen→worker), `QueenInbox` (the queen's callback surface), and `MailboxClient`.
- **`wire.py`** — the codec. `encode`/`decode` plus one constructor per frame type
  (`spawn_msg`, `resume_msg`, `activity_msg`, `heartbeat_msg`, `mailbox_send_msg`, …).
  Pure data. Every field added since the first release is optional and read
  tolerantly (`session_local_id`, `done.reset_at`, `resume.origin`,
  `spawn_rejected.action`/`.origin`), so an older worker on the other end of the
  socket degrades instead of failing.
- **`auth.py`** — an HMAC challenge-response over `SKEP_SHARED_SECRET`, run **once
  per WebSocket connection**, before the register frame. Mutual: both sides prove
  they know the secret. It has nothing to do with Telegram.

`ws_transport.py` implements the protocols over the wire. **The queen is the
server** (aiohttp, path `/ws`); **the worker is the client** and dials out. Workers
never accept inbound connections and never talk to each other. Every node trusts
only the queen. That star topology is a security decision, and §7's org hierarchy
does not change it — manager-to-report messages route *through* the queen.

`transport.py` also carries `SwitchableEventSink` / `SwitchableMailboxClient`,
late-binding indirections so a worker can be constructed before its transport
target exists.

### Persistence — three SQLite databases, not one

| Database | Owner | Holds |
|---|---|---|
| `Registry` (`db.py`) | worker | its own tasks (each an invocation: `resume_token`, `model`, `session_local_id`) + an audit log |
| `Bookkeeping` (`queen/bookkeeping.py`) | queen | `ref → (host, profile, local task id, session id, topic id, message id, status, parked_until)` |
| `Mailbox` (`queen/mailbox.py`) | queen | all inter-agent and agent↔CEO messages |

`Bookkeeping` exists because the Telegram Bot API cannot read topics back — the
queen must remember which topic and which editable message belong to which task.
The mailbox DB is a *sibling file* of the bookkeeping DB, derived from its path in
`queen/assembly.py`. They are not the same database.

Other on-disk state: git worktrees under `worktrees_root`; the agent memory store at
`<repo>/.agent-memory/`; and a per-agent `<worktree>/.skep/mcp.json` (0600, added to
the worktree's git `info/exclude`) holding the MCP server map.

The `Registry` schema is versioned by `PRAGMA user_version`; the v0→v1 migration
renamed `session_id → resume_token` and added `model` + `session_local_id`
(back-filled to each row's own id). This is the worker half of the **Sessions**
model: a task row is now one *invocation*, and invocations sharing a
`session_local_id` are one session's history (`db.py` invocation-grouping queries).

`Bookkeeping` is versioned the same way, and additively: v0→v1 added
`session_local_id` (back-filled to each row's own `local_id`), v1→v2 added
`parked_until`. Both are `ALTER TABLE … ADD COLUMN`, so an older journal opens and
upgrades in place.

---

## 5. Two vocabularies

skep names things from **two metaphors at once**, and neither is wrong, but nothing
in the repo says they coexist. This is the single biggest source of confusion.

**Beehive:** skep (a woven hive), queen, worker, `skepd`.
**Corporate:** CEO, north star, manager, IC, delegate, hire, mentorship, reputation.

The two describe the same system from different angles: the beehive names the
*processes*, the corporate metaphor names the *roles agents play* inside them.

Three places the naming actively misleads:

- **`queen` is not the boss.** It is a non-LLM switchboard: it routes commands,
  owns the bot token, and holds the mailbox. In the org metaphor the boss is the
  **CEO**, which is you. Management is an agent *behavior*, never a queen feature.
- **`onboarding.py` is not hiring.** It is the queen registering itself with its
  Telegram control group. The corporate metaphor makes the wrong reading tempting.
- **`bookkeeping` and `assembly` are not corporate either.** Bookkeeping is a
  lookup table; assembly is dependency wiring.

And two words carry two meanings each: **auth** (`auth.py` = worker↔queen HMAC;
`is_owner` = Telegram) and **CEO** (you, the human; and `ceo`, a literal mailbox
address).

---

## 6. Glossary

Status is one of **live** (exists in code), **design** (specified, not built), or
**superseded**.

### Processes and roles

| Term | Meaning | Lives in | Status |
|---|---|---|---|
| **agent** | One ephemeral `claude -p` process for one task, in its own worktree. | `agent.py:AgentProcess` | live |
| **worker** | The long-running per-(host, profile) daemon that spawns and manages many agents. | `skepd`, `worker/app.py` | live |
| **supervisor** | The class *inside* a worker that actually runs its N concurrent agents. | `supervisor.py:Supervisor` | live |
| **queen** | The single non-LLM process owning Telegram, routing, and the mailbox. | `skep-queen`, `queen/app.py` | live |
| **CEO** | You. Also the literal mailbox address `ceo`. | `queen/addressing.py` | live |
| **IC** | Org name for today's per-task agent: spawned, reports, dies. | = an `AgentProcess` | live (untagged) |
| **manager** | A *durable identity* (role, prompt, inbox, memory) the queen persists and rehydrates into an ephemeral agent on demand. Never a long-running process. | address `mgr:<name>` accepted; no manager state | design (L2) |

> **agent vs worker:** many agents per worker. **worker vs supervisor:** the worker
> is the deployable daemon, the supervisor is its execution engine. Extra workers
> exist for *profile isolation*, not throughput — parallelism is intra-worker.

### Transport

| Term | Meaning | Lives in | Status |
|---|---|---|---|
| **transport seam** | The abstract interfaces letting core logic run over an in-memory fake or a real socket. | `transport.py` | live |
| **wire / wire protocol** | The JSON frames on the WebSocket. | `wire.py` | live |
| **`CommandSource`** | What the specs call the queen→worker interface. | — | superseded name; code says `CommandHandler` |
| **capacity / capacity cap** | Per-worker `max_concurrent` (default 8). | `WorkerConfig`, `Supervisor` | live |
| **topic** | A Telegram forum topic; one per task, deleted on completion. | `telegram_gw.py` | live |

### Mailbox (L0)

| Term | Meaning | Lives in | Status |
|---|---|---|---|
| **mailbox** | Queen-routed, agent-addressed async messaging with a durable inbox — email for agents. | `queen/mailbox.py` | live |
| **addressing** | Resolving a recipient string to `ceo`, `mgr:<name>`, or an IC ref. | `queen/addressing.py` | live |
| **MCP shim** | Per-agent HTTP MCP server exposing `send_message` / `read_inbox`, bearer-token guarded. | `worker/mcp_shim.py:MailboxShim` | live |
| **at-least-once, archive-not-delete** | Persist before ack; `read_inbox` marks rows read but never deletes. | `queen/mailbox.py` | live |
| **loop prevention / depth cap** | Three guards: per-sender rate limit, reply-chain depth cap (dead-letter past 10 hops), content dedupe. | `MailboxService` | live |
| **managers (config)** | `SKEP_MANAGERS` — a static allowlist of names addressable as `mgr:`. Anticipates L2; does not implement it. | `config.py`, `addressing.py` | live |

### Memory (L1.1)

| Term | Meaning | Lives in | Status |
|---|---|---|---|
| **agent memory** | One Markdown file per fact, tracked in git, under `<repo>/.agent-memory/`. Read free at spawn, injected via `--append-system-prompt`. | `memory.py:MemoryStore` | live |
| **memory shim** | A stdio MCP server exposing one tool, `remember`. Child of `claude`, not of skep. | `worker/memory_shim.py` | live |
| **supersession** | A new fact can retire an old one by slug rather than editing it. | `memory.py:write_memory` | live |
| **`MemoryPreflight`, `recall_command`, `probe_memory`** | L1's gortex probe. | — | **superseded, deleted** |
| **sleep cycle / memory defragmentation** | A hypothesized nightly agent that ranks, generalizes, and compacts memory. Explicitly a hypothesis to test, not a foundation. | — | design |

### Isolation

| Term | Meaning | Lives in | Status |
|---|---|---|---|
| **worktree isolation** | Each agent's `cwd` is a fresh `git worktree`. | `Supervisor.spawn` | live |
| **profile isolation** | One worker per Claude profile, so a personal agent can never read work credentials. | `WorkerConfig` | live |
| **env hygiene / default-drop allowlist** | The spawned `claude`'s env is built from a small allowlist (`_CORE_ENV_KEYS` + optional proxy/SSL/`LC_*`), **not** `dict(os.environ)`. Drops the whole `SKEP_*`/`ANTHROPIC_*`/`CLAUDE_CODE_*` namespace so an agent can't read them from its own environ. Widen opt-in via `SKEP_AGENT_ENV_PASSTHROUGH`. | `agent.py:_agent_env` | live (L0.2) |
| **`CLAUDE_CONFIG_DIR` injection** | The mechanism of profile isolation: the variable is set explicitly from the config-dir arg and **never inherited**. | `agent.py` | live |
| **token off argv** | The MCP server map (mailbox bearer + memory stdio) is written to a `0600 .skep/mcp.json` and passed as `--mcp-config <path>`, so the per-agent bearer never appears on `/proc/<pid>/cmdline`. | `worker/mcp_config.py` | live (L0.2) |
| **park & resume** | On a usage-limit hit, mark the session parked-until-reset rather than failed, tell the topic, and auto-resume it when the limit lifts (§3). | `stream.py` (detect), `supervisor.py` (terminal), `queen/telegram_sink.py` (park), `queen/bookkeeping.py` (`parked_until`), `queen/assembly.py` (sweep) | live (A3) |

### Sessions

The multi-provider evolution. **A1 (worker-side) is live. A2 (queen-side) is
*partly* live: the session registry and the `--watch` two-root slice shipped; the
`primary:rw` lease and visibility are still not built. A3 (usage-limit park &
auto-resume) is live — it is what finally gave A2's session machinery a caller.**

| Term | Meaning | Lives in | Status |
|---|---|---|---|
| **session** | A pinned execution context (host/profile/runner/workspace/worktrees) that owns a Telegram topic and is parkable/resumable. Fleet-global `ref`. | `queen/bookkeeping.py` | live (registry A2; parkable/resumable A3) |
| **invocation** | One runner run inside a session; holds a `resume_token` + `model`. A `Registry` row **is** an invocation. | `db.py`, `supervisor.py` | live (A1) |
| **`session_local_id`** | The worker's own session key: a first invocation's == its tid; a resume reuses the origin's. Carried through the worker's register/heartbeat `active_tasks` payload and the queen's replay loop (`QueenWsServer._replay_active`), and through `Bookkeeping` (`session_local_id` column, `by_session()`, `rebind_invocation()`). | `db.py`, `wire.py`, `queen/bookkeeping.py` | live (A1 + A2 registry) |
| **`QueenSink.on_task_started` ref/topic reuse** | Reuses a known session's `ref` and Telegram topic instead of opening a second one — the topic follows the session, via `rebind_invocation`. A3 gave it its callers: every `/resume` and every sweep-driven auto-resume lands here, and `tests/test_integration.py` drives the branch end to end. | `queen/telegram_sink.py` | live (A2), exercised (A3) |
| **`spawn_workspace` / `resume`** | Multi-root + model + `session_local_id` spawn; `resume` = a new invocation on the same worktree from a stored `resume_token` (v1-minimal: token+model+`BASE_TOOLS`, no memory/mailbox). A3 added the dedup claim: `_live_sessions` rejects a second resume of a session that already has one in flight. | `supervisor.py` | live (A1; dedup A3) |
| **`Root` / `Workspace`** | Value types for a multi-root workspace; `requires_lease` flags a `primary:rw` root. A1 ships the predicate; **the lease is still not built — A2 refuses `primary:rw` outright rather than granting it.** | `workspace.py` | live (A1); lease still open |
| **`worker/roots.py:resolve_roots`** | The security gate: maps root NAMES (never paths) from the wire to paths under the worker's own `repos_root`; refuses (never downgrades) bad names, unknown mode/access, `primary:rw`, `attach`, a non-`new` head root, empty/malformed specs. | `worker/roots.py` | live (A2) |
| **`--watch`** | `/spawn <host> [--profile p] <repo> [--watch] <task>` — opt-in two-root workspace (own worktree rw + the repo's primary checkout ro), so the agent can see uncommitted work in the operator's tree. Opt-in because a watched checkout may expose secrets never meant for an agent. | `app.py:watch_roots`, `parse_spawn` | live (A2) |
| **`parked` / `parked_until`** | The fourth terminal (§3): live-but-idle after a usage limit, with a POSIX wake-up deadline on the journal row (`Bookkeeping` schema v2). Counted as *active*, so `/ls` lists it. | `supervisor.py`, `queen/bookkeeping.py` | live (A3) |
| **`detect_usage_limit`** | The one predicate that decides whether a `result` is a usage limit, and what its reset is. Deliberately the only place that changes when a real limit payload is finally captured. | `stream.py` | live (A3), **heuristic** — see §9 |
| **park sweep** | The queen's periodic auto-resume loop (`SKEP_PARK_SWEEP_INTERVAL`, 30 s): resume every due-parked session whose worker is online. Runs in both runtime shapes. | `queen/assembly.py:_park_sweep_loop` | live (A3) |
| **`resume` frame / `cmd_resume`** | The queen→worker sibling of `spawn`: ids only (`session_local_id`, optional `model`, `origin`). Driven by `/resume <ref> [--model]` and by the sweep. | `wire.py:resume_msg`, `queen/router.py` | live (A3) |
| **`origin="sweep"`** | Tag riding a `resume` dispatch and echoed back on `spawn_rejected`, so a machine-driven rejection is logged rather than posted to the owner. | `wire.py`, `queen/telegram_sink.py` | live (A3) |

> **A1 delivered capability, not behavior**, and A2 only partly changed that: its
> ref/topic-reuse branch had no command path to reach it. **A3 is what wired the
> behavior** — the queen now owns `cmd_resume` and a background sweep, and
> `Supervisor.resume` finally has callers. The A1-era claim that "the queen is
> untouched except one ride-along wire field" is history, not current fact.

### Vision

| Term | Meaning | Status |
|---|---|---|
| **north star** | skep as an autonomous agent "company," you as CEO. Set 2026-07-05. | design |
| **delegate** | A manager's `delegate(role, task)` → queen brokers a spawn → result routes back. | design (L3) |
| **earned autonomy** | New managers start gated; a track record widens their budget envelope. | design (L4) |
| **Vasya** | The owner's *other* project — a voice assistant. A possible queen-side integration surface. Not skep code. | external |

---

## 7. Where we are: two numbering axes

**These are orthogonal. Reading them as one sequence guarantees confusion.**

**Phase 1–4** is the build phasing of the control plane:

| Phase | | |
|---|---|---|
| 1 | Telegram-driven single process spawning agents | shipped |
| 2 | Queen + isolated workers, WebSocket, mDNS | shipped |
| 3 | Talk-back + brakes (`ask_human`, gated ops) | not built |
| 4 | Sandbox (container per agent) | not built |

**L0–L5** is the capability ladder toward the north star. The "L" is **Layer**.

| Layer | | |
|---|---|---|
| L0 | **Mailbox** — agent-addressed messaging | shipped (+ L0.1 hardening, + L0.2 Inc 1) |
| L1 | **Agent memory** | shipped, then **superseded by L1.1** |
| L1.1 | Memory as tracked repo files | **shipped & merged** |
| L2 | **Persistent managers** — durable identity, rehydrated on demand | not built |
| L3 | **Delegation** — `delegate` → brokered spawn → result | not built |
| L4 | **Earned autonomy + reputation** | not built |
| L5 | **Mentorship** — mostly L1 applied | not built |

**← you are here:** L0 through L1.1 are merged, and so is **Sessions A2**'s first
slice: the queen-side session registry (`Bookkeeping` session-scoping,
`session_local_id` through register/heartbeat replay, `QueenSink.on_task_started`
ref/topic reuse) and the `/spawn --watch` two-root workspace
(`worker/roots.py:resolve_roots`, the rw-only memory-shim binding, the READ-ONLY
declaration). **Sessions A3** — usage-limit park & auto-resume (§3) — is on this
branch, not yet merged to `main`. Still open within A2: the `primary:rw` lease
table and `visible`/`spawn_visibility` enforcement — plus sub-project C's fleet
catalog and D/E, which neither A2 nor A3 touched. Deferred out of A3 with reasons
in its spec: **P2** the multi-account pool (blocked on an unverified
credential-injection mechanism) and **P3** per-subagent model selection.
**L0.2 Increment 2** (PID/mount namespaces for same-UID containment) is separately
not started.

L0 *depends on* Phase 2: the mailbox rides the transport seam. That dependency is
why the two axes look tangled.

**L1 has been redefined twice.** It began as "sqlite-vec shared memory behind a
`MemoryStore` seam," became "agent memory *is* gortex memory; skep stores nothing"
(L1 spec), and is now "agent memory is tracked files in the repo; skep owns the
write path" (L1.1). The stated reason for the last move: *memory you cannot inspect
is memory you cannot trust.*

Two sub-tiers exist only as commit messages, not in the table: **L0.1** (mailbox
hardening — shipped in two merges) and **L0.2** (per-agent isolation). L0.2 is split:
**Increment 1** (env hygiene + MCP token off argv) is *shipped*; **Increment 2** (PID/
mount namespaces — the only thing that closes the same-UID sibling vector) is *not
started*. Increment 1 confirmed on real `claude`; its honest residual is that a
same-UID sibling can still read the worker's `/proc/<pid>/environ` or the 0600 file.

A **third axis**, orthogonal again, is **Sessions** (see §6): a multi-provider
evolution split into sub-projects A–E. **A1** (worker-side invocations) and the
first **A2** (queen-side) slice — the registry and `--watch` — are merged; A2's
lease and visibility pieces are not. **A3** (usage-limit park & auto-resume) is
built on this branch and awaiting merge. B–E (runner seam, capability catalog,
session spawning, Telegram role) are unstarted.

---

## 8. Map of the documents

`docs/superpowers/plans/` holds **build instructions, executed once**. They are
history. Do not read them to learn how skep works; they describe how it was made,
task by task, and they were accurate only on the day they ran.

`docs/superpowers/specs/` holds designs. Read them for *why*, not *what*:

| Spec | Read it for | Status |
|---|---|---|
| `2026-07-04-agent-fleet-control-plane-design.md` | The original phasing, security model, interrupt constraints | partly superseded (its Phase 2 was re-scoped) |
| `2026-07-04-skep-phase2-queen-workers-design.md` | Queen/worker topology, the seam, wire protocol | live |
| `2026-07-05-l0-mailbox-design.md` | Addressing, delivery guarantees, loop prevention | live |
| `2026-07-05-l0-mcp-shim-spike.md` | Why the shim is shaped as it is | live, but **its topology decision was reversed at build time** |
| `2026-07-09-l1-memory-substrate-design.md` | — | **superseded by L1.1** |
| `2026-07-09-l1.1-agent-memory-files-design.md` | The current memory design | live |
| `2026-07-10-sessions-design.md` | Session/Invocation/Manager model; the A–E split | live (A1 + A2 registry/`--watch` + A3 park/resume built; A2's lease and visibility still design; B–E design) |
| `2026-07-22-sessions-a2-queen-sessions-design.md` | The queen-side session registry and the `--watch` workspace | live (lease + visibility still design) |
| `2026-07-24-sessions-a3-usage-limit-park-resume-design.md` | Why park-and-sweep rather than per-session timers; the two runtime shapes' rejection contracts; the deferred P2/P3 | live (built) — but **§5/§6 were corrected mid-build**: the status check was originally written as if it serialised concurrent resumes, and it does not (§3 above, §9 below) |
| `2026-07-16-l0.2-increment1-...` (plan) | How env hygiene + token-off-argv were built | history (executed) |

`.claude/memory/project.md` is the decision log, and it is the **only** place the
north star and the L0–L5 ladder are written down. It is the richest document in the
repo and the least discoverable.

---

## 9. Sharp edges

Things that will mislead a reader, including a future you. None are bugs; all are
worth knowing before you touch the code.

- **`skep/app.py` is not "the app."** It is single-process mode — but it also owns
  `build_dispatcher`, which the real queen daemon imports. The queen depends on a
  module that looks like a legacy launcher.
- **`RemoteWorker.spawn` always returns `0`.** The WebSocket spawn is
  fire-and-forget; the real task id is assigned asynchronously on the worker. The
  `-> int` in the `CommandHandler` protocol is meaningless on that path.
- **The specs say `CommandSource`; the code says `CommandHandler`.** Same concept,
  renamed, specs never updated.
- **The shim spike was reversed at build time.** It decided one HTTP server per
  worker multiplexed by token; the build shipped one server per agent on an
  ephemeral port. Both the spike and the built code are in the repo.
- **`stream.parse_event` only inspects the first content block** of an assistant
  turn. Multi-block turns collapse to one event. Deliberate, but surprising.
- **L1.1 spec §8.1 marks "two MCP servers coexist in one spawn" as Proven. It is
  not.** The integration test is committed but gated behind `SKEP_RUN_INTEGRATION=1`
  and has never run. Treat the claim as coded, not verified.
- **README's "Agent memory" section describes L1** — the gortex daemon, `skep stores
  nothing`, a preflight probe. L1.1 has merged, so it is now **wrong**: memory is
  tracked repo files with a `remember` shim. The rewrite is still owed (tracked in
  `.claude/memory/project.md`).
- **README documents 7 environment variables; `config.py` reads more** (the count
  keeps growing — `SKEP_AGENT_ENV_PASSTHROUGH` is the newest). Treat README's env
  list as illustrative, not complete.
- **`.skep/mcp.json` has a fixed filename.** Safe today because every root is
  `MODE_NEW` with a tid-unique worktree. The moment attach/`primary` roots go live
  (Sessions A2 / L0.2 Inc 2), two concurrent agents against a shared checkout would
  clobber each other's token file → silent 401. Must become tid-keyed
  (`mcp-<tid>.json`) before then; flagged at the write site in `supervisor.py`.
- **The A1→A2 handoff gap flagged in the A1 review is now closed.** The register-
  replay payload in `ws_transport.py` (`QueenWsServer._replay_active`) carries
  `session_local_id`, so a reconnecting A2 queen no longer loses session identity
  on replayed active tasks. (It was still open as of the previous stamp; this is
  no longer a sharp edge, kept here only as the paper trail.)
- **`cmd_resume`'s `status != 'running'` check looks like a concurrency guard and
  is not one.** It never writes status, so it cannot exclude anything; it is a cheap
  filter. The real dedup is `Supervisor.resume`'s `_live_sessions` claim (§3). The A3
  spec asserted the wrong thing here before it was corrected mid-build — if you find
  that claim anywhere else, it is stale.
- **The usage-limit detector is a text heuristic that has never met a real usage
  limit.** `stream.detect_usage_limit` matches `raw["subtype"] == "usage_limit"` or
  the strings `"usage limit reached"` / `"usage limit exceeded"` — all guesses. The
  real payload was never captured (A3 spec §8.1), and every test drives a synthetic
  event. Treat A3's detection as **coded, not verified**, the same way the L1.1 claim
  above is. If the provider's wording drifts, a limit silently becomes `failed` again
  — no worse than pre-A3, but silent. `detect_usage_limit` is the single change point
  when a real event is finally captured.
- **Sweep-origin rejection suppression is total, and there is no give-up counter.**
  `QueenSink.on_spawn_rejected` drops (logs at INFO) every rejection tagged
  `origin="sweep"`. That is deliberate — a full worker would otherwise page the owner
  every 30 s forever — but it also means a *permanently* failing parked entry (say
  `"no such session"` after a worker's DB was wiped) retries every tick, forever,
  and the owner is never told. Nothing counts attempts and nothing gives up. Note
  the exception type is not a usable discriminator either: `"already has a live
  invocation"` is a benign, expected `ValueError` on the same path.
- **`cmd_resume` routes on worker *registration*; the sweep additionally gates on
  `is_online`.** Deliberate — a bulk background loop should skip disconnected
  workers, while an explicit human `/resume` should not be silently swallowed — but
  it reads as an inconsistency, and it means the two callers of the same method have
  different reachability rules.
- **`RemoteWorker.spawn` returning `0` is load-bearing for Sessions.** The queen mints
  a session `ref` only *after* `task_started`, so at first spawn there is no ref to key
  by — which is exactly why the worker owns `session_local_id` and A2 maps ref→it,
  rather than inverting the fire-and-forget protocol.
