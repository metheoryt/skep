# skep L0 — Mailbox (Design)

**Date:** 2026-07-05
**Status:** Approved design, pre-implementation
**Relationship:** First spec of the "skep as an autonomous agent company" north
star (recorded in `.claude/memory/project.md`). The mailbox is layer **L0** — the
foundation every later layer sits on:

| Layer | Depends on L0? | |
|---|---|---|
| **L0 Mailbox** (this doc) | — | queen-routed agent-addressed messaging |
| L1 Shared memory (A/B/C) | yes — reuses L0's access path | sqlite-vec behind a `MemoryStore` seam; incl. consolidation/"sleep" cycle |
| L2 Persistent managers | yes — durable inboxes drain here | durable identity + on-demand rehydration |
| L3 Delegation | yes — delegate = a routed message + brokered spawn | `delegate(role, task)` → result routed back |
| L4 Earned autonomy + reputation | yes — ACLs land here | competence metrics gate the autonomy envelope |
| L5 Mentorship | yes | expertise transfer via memory promotion + role seeding |

It is also, literally, the "email for agents" the owner originally asked about,
and it forces us to solve **addressing + delivery + loop-prevention** — the exact
gap the 2026-07-05 agent-comms survey flagged as unsolved by any existing standard
(see `.claude/memory/project.md` → "Agent-comms prior-art survey").

## 1. Context

The fleet is a **star topology**: workers connect only to the queen; there is **no
worker↔worker** transport (a security decision — every node trusts exactly one
peer). The org hierarchy of the north star is *logical*, so all inter-agent
messages route **through the queen as a switchboard**. The queen stays **non-LLM**:
the mailbox adds a routing table + an inbox table, not intelligence.

L0 is buildable on today's skep (queen + ephemeral per-task agents + Telegram)
because it is **pull, not push** — see §3.

## 2. Scope & non-goals

**In scope:** agent-addressed messaging routed by the queen; a durable inbox store;
`send_message` / `read_inbox` tools delivered to agents via a worker-local MCP shim
over the existing transport seam; addressing for `ceo` / `mgr:<name>` / IC `ref`;
at-least-once delivery with archive-not-delete; loop-prevention (rate limit +
reply-chain depth cap + content dedupe); dead-lettering; CEO ↔ Telegram
integration.

**Non-goals (deferred, so we don't fake un-designed machinery):**
- Manager rehydration / draining of `mgr:` inboxes → **L2**.
- Role-based ACLs (who may talk to whom) → **L4**. L0 ACL is minimal: any live
  agent may message any valid address, guarded only by rate-limit + depth-cap.
- Proactive push *into* a running agent ("stop, new priority") → **P3 soft-steer**
  (needs stdin stream-json; the `--input-format` stdin-blocking gotcha applies).
- Real WebSocket + real Telegram wiring is exercised at integration time when
  Phase-2 Plan 2's WebSocket lands; L0 unit tests use the in-memory seam + fakes.

## 3. Architecture

**Core decision — the mailbox is PULL.** An agent *checks its inbox* by calling a
tool; the queen never shoves a message into a running agent's process. Rationale:

1. **Sidesteps P3.** Pushing into a live agent means writing stream-json to its
   stdin — the soft-steer mechanism (and its stdin-blocking gotcha), not built.
   Pull needs none of it.
2. **It is genuinely "email"** — async, addressed, you-check-it.
3. **Shares one access path with L1.** Both ride the **worker-local MCP shim →
   authenticated transport → queen**. L0 builds the pipe L1 reuses.

```
  CEO (you)                         ┌───────────── QUEEN (non-LLM) ─────────────┐
     │  Telegram                    │  • message router                          │
     ▼                              │  • inbox store (SQLite, durable)           │
 ┌─────────┐   send/reply          │  • address resolution (reuses bookkeeping) │
 │ Telegram │◀─────────────────────▶  • loop-prevention (rate/depth/dedupe)     │
 │ gateway  │  (queen owns this)    └───────▲───────────────────────▲───────────┘
 └─────────┘                        auth'd seam│                      │auth'd seam
                                    ┌──────────┴─────┐      ┌─────────┴──────┐
                                    │  Worker A      │      │  Worker B      │
                                    │  ┌──────────┐  │      │  ┌──────────┐  │
                                    │  │ MCP shim │  │      │  │ MCP shim │  │
                                    │  └────▲─────┘  │      │  └────▲─────┘  │
                                    │  localhost     │      │  localhost     │
                                    │  ┌────┴─────┐  │      │  ┌────┴─────┐  │
                                    │  │  agent   │  │      │  │  agent   │  │
                                    │  └──────────┘  │      │  └──────────┘  │
                                    └────────────────┘      └────────────────┘
```

The MCP shim is a small per-worker server the spawned agent connects to on
`localhost`; it forwards tool calls to the queen over the existing
`EventSink`/`CommandSource` transport seam (in-memory in Phase-2 Plan 1, WebSocket
in Plan 2). No new public endpoint; no new trust surface.

## 4. Addressing

Three address kinds:

| Address | Resolves to | Notes |
|---|---|---|
| `ceo` | the owner (Telegram chat) | messages here → queen formats + sends to Telegram; owner replies route back |
| `mgr:<name>` | a manager identity | valid queue target in L0 (messages accumulate `unread`); **draining on rehydration = L2** |
| `<ref>` (IC) | a live per-task agent | resolved via the **existing** queen bookkeeping `ref → (host, profile, local_task_id, topic_id, msg_id)` |

L0 needs **no new registry**: `ceo` is static, IC refs reuse bookkeeping, and
`mgr:` names are config-declared valid targets.

## 5. Data model

**One `messages` table** on the queen (alongside the existing bookkeeping SQLite).
The durable-manager vs ephemeral-IC distinction is **not** a storage difference —
it is only *what happens when the recipient dies* (§9).

Envelope:

| Field | Source | Purpose |
|---|---|---|
| `id` | queen-assigned | dedupe + read-cursor + threading |
| `from` | **queen-assigned, never client-supplied** | authenticity (§11) |
| `to` | caller | recipient address |
| `subject`, `body` | caller | the message (`body` capped, see §9) |
| `created_at` | queen-stamped | ordering |
| `in_reply_to` | caller (optional) | threading + reply-chain depth |
| `hops` | queen-computed | loop-prevention (§8) |
| `status` | queen | `unread` → `read`, or `dead_letter` |
| `dead_letter_reason` | queen | why undeliverable |

An address's inbox = `SELECT … WHERE to = <addr> AND status = 'unread' ORDER BY
created_at`.

## 6. Access path — the tools

Two MCP tools, delivered to the agent by the worker-local shim:

- **`send_message(to, subject, body)`** — returns success only after the queen has
  persisted the row (persist-before-ack). Returns a structured error on invalid
  `to`, rate-limit, or oversize.
- **`read_inbox()`** — returns unread messages addressed to the caller in order,
  and marks them `read` in the same transaction. No `wait_for_reply` (YAGNI;
  polling covers it).

The shim is bound to exactly one agent — it tags every call with the `ref` the
queen assigned at spawn, which the queen uses to set `from` (§11).

## 7. Delivery guarantees — at-least-once, archive-not-delete

- **Persist-before-ack:** an accepted `send` is durable and survives a queen
  restart (it is in SQLite).
- **`read_inbox` marks rows `read` transactionally but never deletes them** —
  `read` is an *archive* state. This defuses the nasty edge (queen marks read, the
  return trip dies, the agent never saw them): nothing is truly lost, and the CEO
  can always query "what was in ref 42's inbox."
- A crash *before* the mark commits re-delivers on the next read (at-least-once);
  duplicates are caught by the dedupe rule (§8).
- If stricter delivery is ever needed, an SQS-style **visibility-timeout** upgrade
  slots behind `read_inbox` with no caller change.

## 8. Loop-prevention — 3 layers, defense-in-depth

Mirrors the discipline already recorded for the cross-fleet bot-to-bot edge
(dedupe, rate-limit, depth cap), now applied intra-fleet.

1. **Per-sender rate limit** *(primary, guaranteed ceiling)* — queen caps
   N messages/window per `from` (default ≈20/min, configurable). Over the cap → the
   `send` tool returns an error to the agent ("rate-limited, slow down"); sustained
   breach → CEO alert. Catches any runaway regardless of causality.
2. **Reply-chain depth cap** — a reply (`in_reply_to = X`) gets `hops = X.hops + 1`;
   a fresh message is `hops = 0`. `hops > cap` (default ≈10) → drop + dead-letter +
   CEO notify ("thread hit depth cap"). Catches A↔B↔A escalation.
3. **Content dedupe window** — identical `(from, to, body)` within ≈60s → dropped
   as a duplicate. Catches tight spin loops.

All three thresholds are config, injected with a testable clock.

## 9. Error / edge handling

| Situation | Behavior |
|---|---|
| Unknown / invalid `to` | `send` **fails fast** with an error to the agent — never a silent drop |
| Recipient IC already dead | unread rows → `dead_letter`, surfaced to CEO (L0) / spawning manager (L2) |
| `to = ceo` but Telegram API fails | row stays `unread`; queen retries with backoff (reuses existing Telegram send + `formatting.escape_md`) — not lost |
| Worker / transport drops mid-send | not acked → agent tool retries; persist-before-ack ⇒ at-least-once (dup caught by dedupe) |
| Oversized `body` | rejected over a cap (≈16 KB) |
| Spoofing | impossible — `from` is queen-assigned from the shim's bound `ref` (§11) |
| CEO Telegram reply | queen resolves target via topic→ref bookkeeping / `in_reply_to` → enqueues; dead target → queen asks CEO "that agent finished — spawn a new one?" |

## 10. CEO (Telegram) integration

The queen already owns all Telegram I/O and MarkdownV2 formatting. `to = ceo`
messages are formatted with `formatting.escape_md` (every dynamic value escaped, per
the non-negotiable MarkdownV2 convention) and sent to the owner. The owner's replies
in Telegram are resolved to a target address (via the per-task topic → `ref`
bookkeeping, or `in_reply_to`) and enqueued to that inbox — a normal message with
`from = ceo`.

## 11. Security

- **`from` is queen-assigned, not client-supplied.** The shim is bound to one agent
  (the `ref` handed to it at spawn), so every call is stamped server-side. An IC
  **cannot** claim to be `ceo` or another manager. This is the mailbox's analog of
  the non-negotiable owner-lock: authenticity is structural, not self-declared.
- No new transport or trust surface — the shim rides the existing authenticated
  seam (mutual challenge-response HMAC over WS in Plan 2). The plain-LAN ws hop
  remains the known weak hop, unchanged by L0.
- Loop-prevention (§8) also bounds a compromised or malfunctioning agent's blast
  radius on the messaging plane.

## 12. How L0 supports the later layers

- **L1 (memory):** adds `memory_write` / `memory_search` tools to the *same* shim,
  routed to the queen the same way. No new access path.
- **L2 (managers):** a dormant `mgr:<name>` inbox accumulating `unread` rows is the
  trigger — the queen's "message arrived for a dormant manager → request a
  rehydrate-spawn" rule drains it. L0 leaves the inbox ready; L2 adds the draining.
- **L3 (delegation):** `delegate(role, task)` is a routed message that also brokers
  a spawn; the IC's result routes back to the manager's inbox as a normal message.
- **L4 (ACL/reputation):** the minimal "any→any" rule is replaced by role-scoped
  send permissions; rate-limit/budget envelopes become per-manager and
  reputation-driven.

## 13. New / changed files (indicative)

- `src/fleet/queen/mailbox.py` — inbox store, address resolution, delivery,
  loop-prevention, dead-lettering.
- `src/fleet/queen/*` — wire the mailbox into the queen's command/event handling
  and the Telegram gateway (`ceo` in/out, reply routing).
- `src/fleet/worker/mcp_shim.py` — per-worker localhost MCP server exposing
  `send_message` / `read_inbox`, forwarding over the transport seam; bound to the
  agent's `ref`.
- Transport-seam message types for mailbox send/read (extends `EventSink` /
  `CommandSource`).
- Bookkeeping SQLite migration: add the `messages` table.
- Config: rate-limit / depth-cap / dedupe-window / body-cap knobs; declared
  `mgr:<name>` addresses.
- `tests/…` — see §14.

(Exact module paths reconciled against the current `src/` layout during planning.)

## 14. Testing strategy

Test-first, using the **in-memory transport seam** (from Phase-2 Plan 1) so tests
need **no real WebSocket and no real Telegram**. Three fakes: in-memory transport,
a fake Telegram gateway (assert escaping + retry), and an **injected clock**
(rate-limit / dedupe / depth windows are time-dependent → deterministic, no
wall-clock flakiness).

Test groups (each red→green):

1. **Inbox store** — persist-before-ack; `read_inbox` returns unread in order, marks
   `read` transactionally, archives-not-deletes; second read returns nothing new;
   crash-before-mark re-delivers.
2. **Sender authenticity** — a `send` from an IC claiming `from=ceo` is stamped with
   its real `ref`.
3. **Address resolution** — `ceo`→Telegram path; IC `ref`→bookkeeping; `mgr:<name>`→
   accumulates unread (no drain); unknown → fail fast.
4. **Dead-letter** — IC dies with unread → rows flip to `dead_letter` + surfaced.
5. **Loop-prevention** (heaviest coverage — the survey's unsolved gap): rate limit
   rejects (N+1)th in-window + alerts on sustained breach; reply-chain `hops`
   increment + `hops>cap` drops + dead-letters + notifies; content dedupe drops
   in-window duplicates.
6. **CEO integration** — `ceo` message → MarkdownV2-escaped Telegram send; Telegram
   failure → stays `unread` + retried; CEO reply → resolves target → enqueues; dead
   target → queen prompts CEO.
7. **Durability** — simulated queen restart: persisted messages survive; `unread`
   stays `unread`.

## 15. Open questions / spikes (resolve during planning/implementation)

- **MCP shim transport binding:** confirm the cleanest way for the per-worker
  localhost MCP server to forward over the existing seam and to bind each agent to
  its `ref` at spawn (env / handshake). Reconcile with how Phase-1 spawns agents.
- **Rate-limit / depth-cap / dedupe-window defaults** — the ≈20/min, depth ≈10,
  ≈60s values are starting points; tune once real traffic exists.
- **`read_inbox` polling cadence** — how/whether to nudge agents to poll (system
  prompt guidance vs a periodic self-reminder) without a push channel. Pure-pull is
  the L0 contract; the ergonomics are a spike.
- **Body cap (≈16 KB)** and whether large payloads should instead be references into
  L1 memory once it exists.

## 16. References

- `.claude/memory/project.md` — north star, decomposition (L0–L5), Kafka rejection,
  usage-limit park-&-resume, profile↔repo binding, the agent-comms survey findings.
- `docs/superpowers/specs/2026-07-04-skep-phase2-queen-workers-design.md` —
  queen/worker topology, the transport seam (§5), identity & routing (§8),
  security (§9), the anticipated "cross-worker via queen" note.
- `docs/superpowers/plans/2026-07-04-skep-phase2-plan1-queen-worker-seam.md` — the
  in-memory transport seam L0 tests build on.
- Agent-comms deep-research report (2026-07-05): AutoGen Core's direct-by-ID +
  topic-indirection addressing (the borrowed model); the finding that no emerging
  standard solves loop-prevention / delivery semantics for a self-hosted WS star.
