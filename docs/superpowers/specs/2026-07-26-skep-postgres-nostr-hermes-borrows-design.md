# Postgres, Nostr, and Hermes borrows — design

**Written 2026-07-26.** Three changes designed together because they touch the
same seams: the transport layer, the persistence layer, and the agent capability
model. Each section can be built independently, but the design must account for
their interactions.

---

## 1. Postgres: one database, three schemas

### 1.1 Why

Three SQLite files (Registry, Bookkeeping, Mailbox) are a product of skep's
bottom-up build order, not a deliberate design. The worker's Registry emerged
first; the queen's Bookkeeping was added because the Telegram Bot API cannot read
topics back; the Mailbox was a third file because it was built as a sibling, not
a table. None of the three reasons still hold:

- Registry is worker-local but the queen already centralizes the other two. A
  single Postgres instance makes the queen the natural state owner, and workers
  become stateless clients that read/write through it.
- Bookkeeping is a workaround for Telegram's API. If the transport surface
  changes (§3), most of its columns evaporate.
- Mailbox as a sibling file was an implementation convenience, not a separation
  of concerns.

A single Postgres instance with three schemas (`registry`, `bookkeeping`,
`mailbox`) gives us:

- **Proper concurrency.** SQLite WAL is single-writer. Postgres handles
  concurrent queen operations (spawn + sweep + CEO retry + heartbeat replay)
  without contention.
- **Rich querying.** JSONB for event payloads, full-text search for the audit
  log, proper foreign keys between schemas (`registry.invocations.session_id →
  bookkeeping.sessions.id`).
- **Single backup target.** `pg_dump` once, not three files on two machines.
- **Future-proof.** Skills, memory, cron jobs, delegation state, and credential
  pools all land in the same database with their own schemas.

### 1.2 Schema design

```sql
-- ── registry ── worker-local state, now queen-hosted

CREATE SCHEMA registry;

CREATE TABLE registry.workers (
    id          SERIAL PRIMARY KEY,
    host        TEXT NOT NULL,
    profile     TEXT NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_heartbeat TIMESTAMPTZ,
    UNIQUE (host, profile)
);

CREATE TABLE registry.invocations (
    id              SERIAL PRIMARY KEY,
    worker_id       INT NOT NULL REFERENCES registry.workers(id),
    session_id      INT NOT NULL,  -- FK below, after bookkeeping.sessions exists
    resume_token    TEXT,          -- NULL for first invocation of a session
    model           TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    terminal        TEXT,          -- 'done' | 'failed' | 'killed' | 'parked'
    exit_code       INT,
    FOREIGN KEY (session_id) REFERENCES bookkeeping.sessions(id)
);

CREATE INDEX idx_invocations_session ON registry.invocations(session_id);

-- ── bookkeeping ── session/ref tracking

CREATE SCHEMA bookkeeping;

CREATE TABLE bookkeeping.sessions (
    id              SERIAL PRIMARY KEY,
    ref             INT NOT NULL UNIQUE,  -- the global opaque ref
    worker_id       INT NOT NULL REFERENCES registry.workers(id),
    repo            TEXT NOT NULL,
    task            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running',
        -- 'running' | 'parked' | 'done' | 'failed' | 'killed'
    parked_until    TIMESTAMPTZ,
    topic_id        INT,          -- Telegram-specific; nullable (survives surface change)
    message_id      INT,          -- Telegram-specific; nullable
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_sessions_ref ON bookkeeping.sessions(ref);
CREATE INDEX idx_sessions_status ON bookkeeping.sessions(status);
CREATE INDEX idx_sessions_parked ON bookkeeping.sessions(parked_until)
    WHERE status = 'parked';

-- ── mailbox ── agent-addressed messaging

CREATE SCHEMA mailbox;

CREATE TABLE mailbox.messages (
    id              SERIAL PRIMARY KEY,
    from_addr       TEXT NOT NULL,     -- 'ceo' | 'mgr:<name>' | 'ref:<N>'
    to_addr         TEXT NOT NULL,
    body            TEXT NOT NULL,
    in_reply_to     INT REFERENCES mailbox.messages(id),
    depth           INT NOT NULL DEFAULT 0,
    read            BOOLEAN NOT NULL DEFAULT false,
    dead_letter     BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_mailbox_to_unread ON mailbox.messages(to_addr, created_at)
    WHERE NOT read AND NOT dead_letter;
CREATE INDEX idx_mailbox_dedup ON mailbox.messages(from_addr, body, created_at);

-- ── audit_log ── signed event log (§3 Nostr)

CREATE SCHEMA audit_log;

CREATE TABLE audit_log.events (
    id              TEXT PRIMARY KEY,   -- Nostr event ID (SHA256)
    pubkey          TEXT NOT NULL,      -- author pubkey
    created_at      TIMESTAMPTZ NOT NULL,
    kind            INT NOT NULL,       -- Nostr event kind
    content         JSONB NOT NULL,
    sig             TEXT NOT NULL,      -- Schnorr signature
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_pubkey ON audit_log.events(pubkey, created_at);
CREATE INDEX idx_audit_kind ON audit_log.events(kind, created_at);
```

### 1.3 Migration path

**Phase 1: Queen-side first.** Bookkeeping and Mailbox are already queen-local
SQLite files. Migrate them to Postgres schemas with a one-time dump-and-load.
The queen reads from Postgres; workers are unchanged.

**Phase 2: Worker Registry.** Workers connect to Postgres directly. The worker
gets a `DATABASE_URL` (same as the queen's). `db.py` becomes a thin wrapper over
`asyncpg`. The worker's `spawn`/`resume` write invocation rows; the queen reads
them for heartbeat replay and session tracking. This requires network
reachability from every worker to the Postgres instance — acceptable for now;
revisit if a worker needs to run in a network-isolated environment.

**Phase 3: Drop the SQLite files.** Once Postgres is the source of truth, remove
the old SQLite paths.

### 1.4 Configuration

```bash
# Replaces SKEP_BOOKKEEPING_DB / SKEP_MAILBOX_DB / SKEP_REGISTRY_DB
export DATABASE_URL=postgresql://skep:...@localhost:5432/skep
```

The queen and every worker use the same URL. The queen owns the schema
(migrations); workers connect read/write to their own schemas.

---

## 2. Hermes borrows: seven capabilities

### 2.1 Skills system (procedural memory)

**What it is.** Agents create reusable SKILL.md files — named procedures with
trigger conditions, numbered steps, exact commands, and pitfalls. Skills are
read at spawn (injected into the system prompt addendum) and written through a
new `remember_skill` MCP tool. A background curator prunes, consolidates, and
archives stale skills.

**Where it lives.** `~/.skep/skills/<profile>/` — one directory per Claude profile
(`personal`, `work`). Skills are per-profile, not per-repo: a skill learned on
one repo is available to agents on all repos under the same profile. This
matches the existing isolation model — personal and work profiles have separate
skill sets. Skills are git-tracked in the skep config repo (not in individual
project repos — they belong to the agent's identity, not the project).

**SKILL.md format** (subset of Hermes' format, simplified for agent consumption):

```markdown
---
name: deploy-to-production
description: "Deploy the current branch to production via the CI pipeline."
triggers: [deploy, production, ship]
created: 2026-07-26
updated: 2026-07-26
supersedes: []
---

# deploy-to-production

## When to use
When asked to deploy, ship, or push to production.

## Steps
1. Run `just ci-status` to check the pipeline is green.
2. Run `just deploy-dry-run` to verify the deploy plan.
3. Post the dry-run output to the CEO via `send_message`.
4. Wait for CEO approval (the task will be parked until the CEO replies).
5. Run `just deploy` and monitor the output.
6. Report the result.

## Pitfalls
- Never deploy if `just ci-status` shows a red build.
- The deploy takes ~3 minutes; do not kill the process.
```

**Spawn integration.** Like L1.1 memory, skills are read at spawn and injected
into the system prompt addendum. The addendum grows from:

> Before starting, recall relevant facts from `.agent-memory/`.

To:

> Before starting, recall relevant facts from `.agent-memory/` and check
> `~/.skep/skills/<profile>/` for procedures matching this task. The skills
> directory is available under `SKEP_SKILLS_DIR` in your environment.

**MCP tool: `remember_skill`.** A new tool on the memory shim (or a sibling
shim) that writes a SKILL.md file. Same security model as `remember`: the
shim is a stdio MCP server, child of `claude`, writing to the worktree's
view of the repo.

**Curator.** A queen-side cron job (§2.3) that:
- Walks `~/.skep/skills/<profile>/` periodically.
- Marks skills unused for N days as stale, then archives them.
- Detects overlapping skills and proposes consolidation (via a CEO message).
- Never deletes — max destructive action is archive (move to `.archive/` within the skills directory).

**Schema addition:**

```sql
CREATE SCHEMA skills;

CREATE TABLE skills.curator_state (
    profile     TEXT NOT NULL,
    skill_name  TEXT NOT NULL,
    last_used   TIMESTAMPTZ,
    use_count   INT NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT 'active',  -- active | stale | archived
    PRIMARY KEY (profile, skill_name)
);
```

### 2.2 Persistent cross-session memory (declarative memory)

**What it is.** Durable facts about the user, the environment, and lessons
learned — not repo-scoped, but fleet-scoped. Injected into every agent's system
prompt. Survives across sessions.

**Current state.** skep has no user-level memory. L1.1 is repo-scoped agent
facts. The CEO's preferences, environment quirks, and recurring corrections are
lost between sessions.

**Design.** A single `fleet_memory` table in Postgres, plus a system prompt
addendum that reads from it:

```sql
CREATE SCHEMA memory;

CREATE TABLE memory.facts (
    id          SERIAL PRIMARY KEY,
    content     TEXT NOT NULL,
    source      TEXT NOT NULL,  -- 'ceo' | 'agent:<ref>' | 'curator'
    scope       TEXT NOT NULL DEFAULT 'fleet',  -- 'fleet' | 'repo:<name>' | 'profile:<name>'
    importance  INT NOT NULL DEFAULT 0,  -- 0-10, bumped on reuse
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_by INT REFERENCES memory.facts(id)
);

CREATE INDEX idx_memory_scope ON memory.facts(scope, importance DESC);
```

**Write path.** Two mechanisms:
1. **CEO command.** `/remember <fact>` in Telegram — the queen writes directly.
2. **Agent MCP tool.** `remember_fleet_fact` on the memory shim, writing
   fleet-scoped facts (source = `agent:<ref>`). The agent can only write; it
   cannot read fleet memory at runtime (it's injected at spawn).

**Read path.** At spawn, the system prompt addendum gains a section:

```
## Fleet memory
The CEO has recorded the following preferences and facts:
- <fact 1>
- <fact 2>
```

Facts are filtered by scope: agents on a repo get fleet-scoped + repo-scoped
facts. Profile-scoped facts are gated by the worker's profile.

**Curator integration.** The same background job that curates skills (§2.1) also
runs memory maintenance: bump importance on reused facts, propose supersession
of contradictory facts, archive facts not referenced in N days.

### 2.3 Cron scheduler

**What it is.** A proper scheduler replacing skep's ad-hoc loops (park sweep,
CEO retry). Jobs are defined with a schedule, a prompt or skill set, and a
delivery target.

**Current state.** skep has three hardcoded loops:
- `_park_sweep_loop` (30s) — resume due-parked sessions
- `_ceo_retry_loop` (30s) — redeliver pending CEO mail
- The park sweep runs in both runtime shapes with different wiring

**Design.** A single `cron` schema with a scheduler loop:

```sql
CREATE SCHEMA cron;

CREATE TABLE cron.jobs (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    schedule        TEXT NOT NULL,  -- cron expression or 'every Nh'
    prompt          TEXT,           -- self-contained task description
    skills          TEXT[],         -- skill names to load
    model           TEXT,           -- optional model override
    enabled         BOOLEAN NOT NULL DEFAULT true,
    last_run        TIMESTAMPTZ,
    next_run        TIMESTAMPTZ NOT NULL,
    run_count       INT NOT NULL DEFAULT 0,
    error_count     INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE cron.runs (
    id              SERIAL PRIMARY KEY,
    job_id          INT NOT NULL REFERENCES cron.jobs(id),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    exit_code       INT,
    output          TEXT,
    error           TEXT
);
```

**Built-in jobs** (migrated from hardcoded loops):

| Job | Schedule | What it does |
|-----|----------|--------------|
| `park-sweep` | every 30s | Resume due-parked sessions on online workers |
| `ceo-retry` | every 30s | Redeliver pending CEO mailbox messages |
| `skill-curator` | every 6h | Walk `.agent-skills/`, update curator state, propose consolidations |
| `memory-curator` | every 6h | Bump importance, propose supersessions |
| `credential-rotation` | every 1h | Check credential pool health, rotate exhausted keys (§2.6) |

**Scheduler loop.** Runs in the queen process (both shapes). Replaces the
hardcoded `_park_sweep_loop` and `_ceo_retry_loop` with a single loop that
reads `cron.jobs WHERE next_run <= now()` and dispatches each.

**Dedup.** A `.tick.lock` file (or Postgres advisory lock) prevents duplicate
ticks across queen restarts.

### 2.4 Delegation (L3)

**What it is.** A manager agent can spawn sub-agents (ICs) with
`delegate(role, task)` — the queen brokers the spawn, routes the result back to
the manager. This is skep's L3, designed but unbuilt.

**What Hermes adds.** The `delegate_task` shape is proven: goal + context,
isolated context, parallel batch mode, orchestrator role. The key design
insight: delegation is a **queen-mediated spawn**, not a direct agent-to-agent
call.

**Design.** A new MCP tool on the mailbox shim: `delegate(role, task, context?)`.

1. The manager agent calls `delegate("backend-dev", "Add rate limiting", "...")`.
2. The shim sends a `delegate_request` frame over the WS to the queen.
3. The queen resolves the role to a (host, profile, repo) — initially from a
   static `SKEP_ROLES` config, later from the capability catalog (Sessions C).
4. The queen dispatches a `spawn` to the target worker, tagged with
   `parent_ref=<manager's ref>`.
5. When the IC finishes, the queen routes the result back to the manager's
   mailbox (not the CEO's Telegram topic).
6. The manager reads the result from its inbox and continues.

**Schema addition:**

```sql
CREATE SCHEMA delegation;

CREATE TABLE delegation.roles (
    name        TEXT PRIMARY KEY,
    host        TEXT NOT NULL,
    profile     TEXT NOT NULL,
    repo        TEXT NOT NULL,
    system_prompt TEXT,  -- role-specific system prompt addendum
    max_concurrent INT NOT NULL DEFAULT 3
);

CREATE TABLE delegation.delegations (
    id              SERIAL PRIMARY KEY,
    parent_ref      INT NOT NULL,  -- manager's session ref
    child_ref       INT NOT NULL,  -- IC's session ref
    role            TEXT NOT NULL,
    task            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
        -- 'pending' | 'running' | 'done' | 'failed'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**Concurrency.** A manager can have at most N concurrent delegations
(configurable per role). The queen enforces this — it's the same
`max_concurrent` check as a regular spawn, but scoped to the manager's
delegation budget.

**Scope.** L3 delegation is **intra-fleet** (same queen). Cross-fleet delegation
(L3 stretch) is out of scope.

### 2.5 Approval gates (Phase 3 brakes)

**What it is.** Before an agent executes a high-risk action, the queen pauses
the agent and asks the CEO for approval. The CEO approves or denies in Telegram.
The agent resumes with the answer.

**Current state.** Phase 3 ("talk-back + brakes") is designed but unbuilt. The
`--permission-prompt-tool` flag was removed from `claude` 2.1.201, so the brake
must be a **blocking PreToolUse hook**.

**What Hermes adds.** The `approvals.mode` concept: `smart` (auto-approve safe,
deny dangerous, prompt uncertain), `manual` (always prompt), `off` (YOLO).
Hermes uses an auxiliary LLM for the smart assessment.

**Design for skep.** skep cannot use a PreToolUse hook on Claude Code without
the Agent SDK (which costs subscription auth and profile isolation — see the
Buzz memo §5.1). Instead, skep implements brakes at the **queen level**:

1. The agent's MCP tools (`send_message`, `delegate`, `remember_fleet_fact`) are
   the gated surface. The shim does not gate them — the **queen** does.
2. When the queen receives a gated action, it checks the agent's autonomy level
   (§2.7 earned autonomy).
3. If gated, the queen parks the agent (same mechanism as usage-limit park) and
   sends a Telegram message: "Agent <ref> wants to <action>. Approve?"
4. The CEO replies `/approve <ref>` or `/deny <ref>`.
5. The queen resumes the agent with the answer injected into its context.

**Schema addition:**

```sql
CREATE SCHEMA approvals;

CREATE TABLE approvals.gates (
    action      TEXT PRIMARY KEY,  -- 'delegate' | 'remember_fleet_fact' | 'send_message.to:ceo'
    min_level   INT NOT NULL DEFAULT 0  -- 0 = always allow, 1 = prompt below L1, etc.
);

CREATE TABLE approvals.pending (
    id              SERIAL PRIMARY KEY,
    session_ref     INT NOT NULL,
    action          TEXT NOT NULL,
    payload         JSONB NOT NULL,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    resolution      TEXT  -- 'approved' | 'denied' | NULL (pending)
);
```

**Phase 3 proper** (PreToolUse hook on Claude Code) is deferred until the runner
seam (Sessions B) supports ACP, which has native approval hooks. The queen-level
gates are a working stopgap.

### 2.6 Credential pools

**What it is.** Multiple API keys per provider, rotated automatically. When one
key hits its rate limit, the next key in the pool is used.

**Current state.** skep has one credential per profile (one
`~/.claude`/`~/.claude-work`). Sessions A3 deferred P2 (multi-account pool)
because the credential-injection mechanism was unverified.

**Design.** The queen manages a pool of credentials per profile. At spawn, the
queen selects an available credential and passes it to the worker (never the
agent directly — the worker injects it into the `claude` process environment).

```sql
CREATE SCHEMA credentials;

CREATE TABLE credentials.pools (
    id          SERIAL PRIMARY KEY,
    profile     TEXT NOT NULL,  -- 'personal' | 'work'
    provider    TEXT NOT NULL DEFAULT 'anthropic'
);

CREATE TABLE credentials.keys (
    id              SERIAL PRIMARY KEY,
    pool_id         INT NOT NULL REFERENCES credentials.pools(id),
    key_hash        TEXT NOT NULL,  -- SHA256 of the key; never store the raw key
    exhausted_until TIMESTAMPTZ,    -- NULL = available
    priority        INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**Key storage.** Raw keys live in environment variables:
`SKEP_CREDENTIAL_POOL_PERSONAL=key1,key2,key3`. The queen hashes them for the
database. The worker receives the selected key at spawn time via the wire
protocol (encrypted over the WS). A vault integration (Bitwarden CLI, 1Password
CLI) is a future upgrade path — env vars are sufficient for a single-operator
fleet with a handful of keys.

**Selection.** The queen picks the highest-priority non-exhausted key. When an
agent hits a usage limit (detected by `detect_usage_limit`), the queen marks
that key `exhausted_until <reset_at>` and the park sweep uses the next key on
resume.

**This directly enables the deferred Sessions A3 P2.** The credential-injection
problem (the `claude` process needs the key in its environment) is solved by the
worker receiving the key from the queen and setting it in the agent's env — the
same mechanism as `CLAUDE_CONFIG_DIR` injection.

### 2.7 Curator pattern

**What it is.** A background maintenance system that prunes, consolidates, and
archives agent-created artifacts. Not a single feature — a pattern applied to
skills (§2.1), memory (§2.2), and any future agent-generated content.

**Design.** The `cron.jobs` table (§2.3) holds curator jobs. Each curator is a
queen cron job that runs in-process — same as the park sweep and CEO retry
loops. It is low-risk maintenance work (walk files, update metadata, propose
consolidations), not on the critical path. A crashed tick retries on the next
interval.

Each curator job:

1. **Walks** the target data (skills files, memory facts, delegation history).
2. **Scores** each item (recency, usage count, importance).
3. **Acts** on low-scoring items: mark stale, archive, propose consolidation.
4. **Reports** to the CEO via a mailbox message.

**Invariants:**
- Never delete — max destructive action is archive.
- Every action is logged to `audit_log.events`.
- The CEO can always restore from archive.
- Consolidation proposals are CEO-approved, not auto-applied.

---

## 3. Nostr transport

### 3.1 What Nostr is

Nostr (Notes and Other Stuff Transmitted by Relays) is a simple protocol where:

- Every participant has a keypair (Schnorr signatures).
- Every message ("event") is signed and sent to relays.
- Relays are dumb — they accept events, store them, and forward them to
  subscribers. Relays don't talk to each other.
- Clients subscribe to events by pubkey, kind, or other filters.
- There is no request/reply — it's publish/subscribe.

### 3.2 What it would replace in skep

| skep piece | Nostr replacement |
|---|---|
| `wire.py` (custom JSON frames) | Nostr event kinds (standardized, extensible) |
| `auth.py` (HMAC challenge-response per connection) | Per-event Schnorr signatures (no connection auth needed) |
| `ws_transport.py` (queen WebSocket server + worker client) | Relay (queen and workers are both clients) |
| `transport.py` (EventSink/CommandHandler/QueenInbox protocols) | Event subscriptions (queen subscribes to worker events, workers subscribe to commands) |
| `Bookkeeping` (ref → topic mapping) | Partially evaporates — events are self-describing |
| Audit log (Registry audit table) | The Nostr event log IS the audit log |

### 3.3 What it adds

1. **Per-agent cryptographic identity.** Every agent gets a keypair. Every
   action it takes is signed. "Who did this" is answerable and unforgeable.
   This is the missing substrate under L4 reputation.

2. **A signed, immutable event log.** The relay stores every event. The audit
   trail is the protocol itself — no separate audit table, no "the registry
   says X but the logs say Y."

3. **Portable identity.** An agent's key belongs to the agent, not to skep. If
   the queen changes, the agent's identity and history survive.

4. **Simpler topology.** No WebSocket server, no HMAC handshake, no
   connection-level auth. The queen and workers are both Nostr clients. The
   relay is off-the-shelf infrastructure.

5. **Multiple relays for resilience.** Publish to 2-3 relays; if one goes down,
   the others still have the events. No single point of failure.

### 3.4 Event kinds for skep

Nostr events have a `kind` field (integer). The protocol reserves 0–9999 for
standard kinds; 10000+ are application-specific. skep would use:

| Kind | Name | Publisher | Subscribers | Content |
|------|------|-----------|-------------|---------|
| 10001 | `skep-spawn` | queen | worker | `{host, profile, repo, task, roots, model, session_id}` |
| 10002 | `skep-event` | worker | queen | `{session_id, event_type, data}` (streaming agent output) |
| 10003 | `skep-terminal` | worker | queen | `{session_id, terminal, exit_code, reset_at?}` |
| 10004 | `skep-heartbeat` | worker | queen | `{host, profile, active_sessions, capacity}` |
| 10005 | `skep-resume` | queen | worker | `{session_id, model?}` |
| 10006 | `skep-kill` | queen | worker | `{session_id}` |
| 10007 | `skep-mailbox-send` | worker (via shim) | queen | `{from, to, body, in_reply_to?}` |
| 10008 | `skep-mailbox-deliver` | queen | worker (via shim) | `{to, body, from, in_reply_to?}` |
| 10009 | `skep-delegate` | worker (via shim) | queen | `{parent_session, role, task}` |
| 10010 | `skep-delegate-result` | worker | queen | `{parent_session, child_session, result}` |

### 3.5 What changes in the architecture

**Queen.** Becomes a Nostr client. Publishes `skep-spawn`, `skep-resume`,
`skep-kill` events. Subscribes to `skep-event`, `skep-terminal`,
`skep-heartbeat`, `skep-mailbox-send`, `skep-delegate` from all workers.
No longer runs a WebSocket server. The Telegram gateway is unchanged — it
still receives events from the queen, just now the queen gets them from the
relay instead of from a WebSocket.

**Worker.** Becomes a Nostr client. Subscribes to `skep-spawn`, `skep-resume`,
`skep-kill` events tagged with its `(host, profile)`. Publishes
`skep-event`, `skep-terminal`, `skep-heartbeat`. No longer dials a WebSocket
server.

**Relay.** An off-the-shelf Nostr relay (e.g., `nostr-rs-relay`, `strfry`).
Runs alongside the queen. Accepts events, stores them, forwards to subscribers.
The relay is the message bus.

**Mailbox.** The mailbox shim publishes `skep-mailbox-send` events to the relay.
The queen subscribes to them, processes delivery, and publishes
`skep-mailbox-deliver` events. The shim subscribes to delivery events for its
agent's address. This is publish/subscribe, not request/reply — the shim's
`send_message` returns a pending acknowledgement, and the agent polls
`read_inbox` for replies.

**Bookkeeping.** The `topic_id` and `message_id` columns remain (Telegram still
needs them), but `ref` mapping is now derivable from the event log. A session's
lifecycle is a sequence of signed events — the queen can reconstruct state from
the relay.

### 3.6 What Nostr does NOT solve

These are things the current WebSocket transport provides that Nostr does not,
and that skep must build on top:

1. **Request/reply.** Nostr is publish/subscribe. The mailbox's `send_message →
   ack` pattern and the delegation's `delegate → result` pattern need a
   correlation ID in the event content and a timeout on the client side.

2. **Delivery guarantees.** Nostr relays can drop events. skep needs
   at-least-once delivery for spawn commands and mailbox messages. The fix:
   publish to multiple relays, and include a sequence number so the receiver can
   detect gaps.

3. **Ordering.** Nostr events have timestamps but no total order. skep's
   streaming agent output is ordered by construction. The fix: include a
   per-session sequence number in `skep-event` content.

4. **Connection state.** The current WebSocket gives the queen instant knowledge
   of worker connectivity (connect/disconnect). With Nostr, the queen infers
   liveness from heartbeat events. A worker that stops publishing heartbeats is
   "offline" — but the detection lag is the heartbeat interval, not instant.

5. **Latency.** WebSocket is a direct connection. Nostr adds relay hop latency.
   For streaming agent output (the main data flow), this is acceptable — the
   relay is local, and events are small. For interactive commands (`/spawn`,
   `/kill`), the latency is still sub-second.

### 3.7 Recommendation: adopt Nostr event signing, defer the relay

**Decision: store signed events in `audit_log.events`, skip the relay for now.**
The strongest borrow from Nostr is the event model (per-agent keypair + signed
events), not the relay infrastructure. The relay adds deployment complexity
(another service to run, another thing to back up) for a benefit that only
materializes in a multi-participant deployment. The `audit_log.events` table
gives us the signed event log today; the relay is a drop-in upgrade if skep
ever becomes multi-participant.

**The hybrid design:**

1. **Keep the WebSocket transport** for real-time command/control and streaming
   agent output. It's working, low-latency, and already has mutual auth.
2. **Add Nostr event signing** to every wire frame. The worker signs each event
   with its agent's keypair. The queen verifies the signature. The signed events
   are stored in `audit_log.events` (Postgres).
3. **The audit log is the record, not the transport.** The WebSocket is the
   control plane; the `audit_log.events` table is the tamper-evident history.
   This gives us the signed event log without sacrificing the low-latency,
   ordered, guaranteed-delivery properties of the WebSocket.

**Future Nostr relay path.** If the deployment ever becomes multi-participant,
the audit log can be promoted: run a local `nostr-rs-relay` backed by the same
Postgres instance, publish events to it instead of (or in addition to) writing
them directly, and let the relay handle subscription and fan-out. The signed
event format is the same in both shapes — the relay is just a different
delivery and storage backend for the same events.

### 3.8 Key management

Every worker generates a keypair at startup (or loads one from a file). Every
agent invocation gets a subkey derived from the worker's key (BIP-32 style) so
individual agent actions are attributable to a specific invocation.

The queen has its own keypair. The CEO's Telegram user ID is linked to the
queen's pubkey — the queen signs commands on behalf of the CEO.

Key storage: `0600` files under `~/.skep/keys/`. Never in the database.

---

## 4. Build order

The three changes are designed together but built independently. The dependency
graph is:

```
Postgres (§1)
    │
    ├── Skills (§2.1) ── needs DB for curator state
    ├── Memory (§2.2) ── needs DB for facts table
    ├── Cron (§2.3) ── needs DB for jobs/runs tables
    ├── Delegation (§2.4) ── needs DB for roles/delegations
    ├── Approvals (§2.5) ── needs DB for gates/pending
    └── Credential pools (§2.6) ── needs DB for pools/keys

Nostr event signing (§3.7 hybrid) ── independent of Postgres; adds signing to wire frames
Curator (§2.7) ── depends on skills + memory + cron being built
```

**Recommended sequence:**

1. **Postgres migration** (Phase 1: queen-side only). This unblocks everything
   else. Workers stay on SQLite for now.
2. **Cron scheduler** — replaces the hardcoded loops, gives us the
   infrastructure for curators and credential rotation.
3. **Skills system** — the highest-impact borrow. Agents start creating and
   reusing procedures immediately.
4. **Fleet memory** — the second highest-impact. Preferences survive across
   sessions.
5. **Nostr event signing** — add keypairs and signatures to the existing wire
   frames. No transport change, just signing.
6. **Credential pools** — enables the deferred Sessions A3 P2.
7. **Delegation** — L3, the big one. Requires skills + memory + credential pools
   to be in place.
8. **Approval gates** — Phase 3 brakes. Requires delegation to be meaningful
   (the main gated action is `delegate`).
9. **Curator** — last, because it curates artifacts created by all of the above.

---

## 5. What this does to the north star

The L0–L5 ladder after these changes:

| Layer | | Status |
|---|---|---|
| L0 | Mailbox | **Shipped** (unchanged; Postgres migration is a backend swap) |
| L1.1 | Agent memory (repo files) | **Shipped** (unchanged) |
| L1.2 | Skills (procedural memory) | **New** — §2.1 |
| L1.3 | Fleet memory (declarative memory) | **New** — §2.2 |
| L2 | Persistent managers | Still design (durable identity + rehydration) |
| L3 | Delegation | **Designed** — §2.4 (was unbuilt design; now has a concrete plan) |
| L4 | Earned autonomy + reputation | **Substrate laid** — §3 Nostr signing gives tamper-evident track record |
| L5 | Mentorship | Still design |

Phase 3 (talk-back + brakes) goes from "not built" to "queen-level gates
designed" (§2.5). The full PreToolUse hook on Claude Code is still deferred
until the runner seam (Sessions B) supports ACP.

The credential pool (§2.6) closes the deferred Sessions A3 P2.

The cron scheduler (§2.3) replaces three hardcoded loops with one general
mechanism.

The Postgres migration (§1) replaces three SQLite files with one database.

The Nostr hybrid (§3.7) adds cryptographic identity and a signed event log
without changing the transport. The door to full Nostr transport is left open
behind the Buzz memo's discriminator: "multi-participant workspace ⇒ promote
relay to primary transport."

---

## 6. Resolved decisions

1. **Postgres: workers connect directly.** `DATABASE_URL` is shared between
   queen and workers. Workers read/write to their schemas directly. Simpler than
   queen-proxied; revisit if a worker needs network-isolated operation.
2. **Skills: per-profile.** `~/.skep/skills/<profile>/` — a skill learned on one
   repo is available to all repos under the same profile. Matches the existing
   profile isolation model.
3. **Nostr: signed events in `audit_log.events`, no relay.** The relay is a
   future upgrade path for multi-participant deployments. The signed event
   format is the same either way.
4. **Credentials: env vars.** `SKEP_CREDENTIAL_POOL_<PROFILE>=key1,key2,key3`.
   Vault integration is a future upgrade.
5. **Curator: queen cron job.** Runs in-process alongside the park sweep and CEO
   retry loops. Low-risk maintenance — a crashed tick retries on the next
   interval. No separate process to deploy or monitor.