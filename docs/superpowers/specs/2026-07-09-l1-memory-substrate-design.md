# L1 — Shared Memory Substrate (design)

**Date:** 2026-07-09
**Status:** Approved design, pre-implementation
**Relationship:** Second spec of the "skep as an autonomous agent company" north star
(recorded in `.claude/memory/project.md`). Sits directly on **L0 Mailbox**
(`2026-07-05-l0-mailbox-design.md`), which is built and merged.

| Layer | Depends on | |
|---|---|---|
| L0 Mailbox (done) | — | queen-routed agent-addressed messaging |
| **L1 Memory substrate (this doc)** | L0 — reuses its access path + req/reply layer | scoped, searchable, queen-governed memory |
| L1.5 Consolidation / "sleep cycle" | L1 | rank → generalize → compact. **Deliberately deferred — see §11** |
| L2 Persistent managers | L1 | durable identity + on-demand rehydration |
| L3 Delegation | L2 | `delegate(role, task)` → result routed back |
| L4 Earned autonomy | L1, L3 | competence metrics gate the autonomy envelope |
| L5 Mentorship | L1 | expertise transfer via memory promotion |

L0 §12 already reserved this: *"adds `memory_write` / `memory_search` tools to the
**same** shim, routed to the queen the same way. No new access path."* This spec
honors that literally — there is no new endpoint, no new port, no new trust surface.

---

## 1. What this spec is not

Three things people expect under "shared vector memory" are **out of scope**, each
for a stated reason:

- **The consolidation / "sleep cycle."** It presupposes a populated store with real
  accumulated memory to defragment; you cannot compact an empty database. Project
  memory records "agents get better with experience" as a *hypothesis to test, not a
  foundation to assume* — the sleep cycle **is** that experiment, and shipping it
  alongside the substrate would mean designing the test before the thing it tests.
  Its own spec, once memory is accumulating and there is evidence it bloats. §11
  records the seams it will need.
- **Vectors and embeddings.** See §4.
- **Manager-owned scopes (L2) and reputation-gated ACLs (L4).** No manager identity
  exists yet; inventing an authority tier before its consumer is speculative.

---

## 2. Context and constraints inherited

- **Star topology.** Workers connect only to the queen; there is no worker↔worker
  transport. Memory is queen-hosted; agents reach it through the queen. This is a
  security decision and it survives untouched.
- **Queen is containerized** on the homeserver behind Caddy; **workers are native.**
  The queen has no filesystem access to a worker's repo. This constrains §7.
- **Queen stays non-LLM.** Memory is storage plus policy. Nothing in this layer
  calls a model.
- **Embedded, not a service.** Project memory: *"start embedded for a small fleet
  unless N workers justifies the split"* (the MemClaw-style standalone REST service
  is the deferred alternative). One queen, a handful of workers, `max_concurrent` 8.
- **Profile↔repo binding (owner-confirmed).** The work profile (`~/.claude-work`)
  operates only on work repos; personal (`~/.claude`) on personal repos. §6 extends
  this boundary from tasks to memory.

---

## 3. Architecture

Two layers on the queen, mirroring the split `mailbox.py` already uses — a dumb
store under a policy service:

```
  agent (spawned claude)
     │  memory_write / memory_search   (MCP tool call, localhost, bearer token)
     ▼
  worker: MailboxShim  ──►  MemoryClient  (SwitchableMemoryClient)
     │
     │  WS frames: memory_write / memory_write_ack
     │             memory_search / memory_search_reply
     │  (req_id + Future correlation layer — built in L0, reused as-is)
     ▼
  ┌───────────────── QUEEN (non-LLM) ──────────────────┐
  │  MemoryService   — scope resolution, ACL, caps      │
  │        │            (policy; no SQL)                │
  │        ▼                                            │
  │  MemoryStore     — Protocol (the seam)              │
  │        └── SqliteMemoryStore  → memory.db (FTS5)    │
  │                                                     │
  │  Bookkeeping (existing) — ref, host, profile, repo  │
  └─────────────────────────────────────────────────────┘
```

New module: `src/skep/queen/memory.py` (`MemoryStore` Protocol, `SqliteMemoryStore`,
`MemoryService`, `Entry`, `WriteResult`, `SearchHit`).

`MemoryStore` is the seam that makes §4's decision reversible. Swapping FTS5 for
sqlite-vec replaces exactly one class and touches nothing above it.

### 3.1 Identity is spoof-proof by construction

The mailbox already guarantees an agent cannot forge `from`: the per-agent shim
**closes over its `tid`**, and the queen resolves `tid → ref` through bookkeeping
(L0.1: `mcp_token` per agent, `agent_sender` server-side). Memory reuses this
verbatim. The author of every memory row is derived, never supplied.

---

## 4. Retrieval: FTS5 now, vec-ready

**Decision: SQLite FTS5 (BM25 keyword retrieval) behind the `MemoryStore` seam.**

Verified 2026-07-09 against `platform.claude.com/docs/en/build-with-claude/embeddings`:
**Anthropic offers no first-party embeddings endpoint** and points to Voyage AI
(`voyage-4`, `voyage-code-3`, `voyage-context-4`). sqlite-vec stores and searches
vectors but *generates none*. Choosing it therefore commits the containerized queen
to one of:

- an outbound Voyage call on every write and query — a new API key on the queen, a
  new network dependency, per-token cost, added latency, and memory unavailable when
  Voyage or the network is down; or
- a local embedding model baked into the queen image — a much fatter container, CPU
  and RAM for inference on the homeserver, and a full re-embed of the store on any
  model change.

Neither is justified before there is evidence keyword retrieval misses. At one queen,
a handful of workers, and a small curated store, BM25 over a well-keyed corpus is
plausibly sufficient. The seam makes this a swap, not a rewrite.

**Revisit when** an agent demonstrably fails to retrieve a memory it wrote, using
different words. Log search misses to make that measurable.

**Availability.** FTS5 is a *compile-time* SQLite option, not guaranteed on every
build. Probed 2026-07-09 on this host (SQLite 3.46.1, Python 3.14): `CREATE VIRTUAL
TABLE … USING fts5` and `ORDER BY rank` both work. The queen runs a **different
image**, so `SqliteMemoryStore.open()` asserts FTS5 at startup and raises a clear
error naming the missing feature. Fail loudly at boot, never silently at first write.

---

## 5. Scopes: the agent names a *kind*, the queen resolves the *instance*

This is the central invariant of the design.

An agent calls `memory_write(scope="repo", …)`. The queen expands `"repo"` to
`repo:<repo_key>` using **the calling task's own bookkeeping row**. There is no way
to name `repo:some-other-repo`, `profile:work`, or another task's scratchpad — not
because a check rejects it, but because that address space is **not reachable from
the tool signature**. This is the same structural guarantee that stops an agent
forging a mailbox `from`, and it is strictly stronger than a validation check.

`Bookkeeping.entries` already carries `ref`, `host`, `profile`, `repo` on every task
(verified 2026-07-09). Every instance below resolves from that single row.

| Kind (agent-facing) | Instance (queen-resolved) | Read | Write |
|---|---|---|---|
| `task` | `task:<ref>` | owning task only | owning task |
| `repo` | `repo:<repo_key>` | any task on that repo | any task on that repo |
| `profile` | `profile:<profile>` | any task on that profile | any task on that profile |
| `ceo` | `ceo` | all tasks | **CEO only**, via Telegram |

Resolution is **fail-closed**: an unrecognized kind, or a `ref` whose bookkeeping row
is missing, is rejected — mirroring `addressing.py`'s existing default. Agent writes
to `ceo` are rejected.

### 5.1 Why these four, and why the two axes are not one axis

Owner-settled, 2026-07-09:

- *"Usually different profiles work on different repos, but when they work on the
  same repo, the repo-related things are shared."* → `repo:<repo_key>` is shared
  **across** profiles and hosts.
- *"The global profile memory should be isolated from other profiles."* →
  `profile:<profile>` is isolated **between** profiles.

These are orthogonal axes, which is why a single profile-partitioned "wiki" was the
wrong shape. The north star's "company wiki" is realized as `profile:<profile>`:
each profile is, in practice, its own division.

`ceo` is deliberately **not** partitioned by profile — it is one human with one set of
preferences. If CEO-authored guidance ever needs to be work-confidential, it should be
written into the relevant `profile:` scope, not into a partitioned `ceo`. Revisit only
if that need materializes.

### 5.2 "Operational information" is content, not a scope

**Scopes are ACL boundaries, not topic boundaries.** If a note has the same
permissions as its neighbours, it is a *tag on an entry*, not a new scope. Adding a
scope to express a filter would be inventing ACL machinery for a `WHERE` clause.

So there is no `ops` scope. Operational facts are simply the *content* of the `repo`
and `profile` scopes — things an agent needs that no repo doc should carry: this
stack takes ninety seconds to come up, that test flakes under load, this box wants
`just switch` and not `nix-rebuild`.

### 5.3 The pointer rule (guidance, not an invariant)

Owner-settled: *per-repo knowledge belongs in the repo.* Where the repo can hold a
fact, memory holds a **pointer**, not a copy — duplicating repo content into memory
creates a second source of truth that drifts. This mirrors the rule already written
into the user's own memory guidance ("Don't save what the repo already records").

**This is unenforceable and the spec says so plainly.** Nothing in the store can stop
an agent pasting a `CLAUDE.md` into a memory row. It lives in the `memory_write` tool
*description*, phrased prescriptively about **when** to write — the placement that
measurably lifts correct tool use. It is guidance, not a guarantee.

---

## 6. Repo identity: `repo_key`

Making `repo:*` cross-profile creates a hazard. Bookkeeping stores `repo` as the bare
string from `/spawn <host> <repo> <task>`. Repos are grouped by namespace (`~/my`,
`~/pure`, `~/cyphy671`), so two genuinely different repos can share a basename. If
`~/my/foo` and `~/pure/foo` both resolve to `repo:foo`, a personal-profile agent and a
work-profile agent share a memory scope — and the cross-profile isolation of §5 leaks
through the one scope that is deliberately shared. Silently: no error, just an agent
reading another division's operational notes.

**Decision: key on the git remote URL, falling back to `host:path`.**

The worker computes, at spawn:

1. `git remote get-url origin` → normalize (strip trailing `.git`, lowercase host)
   → e.g. `github.com/metheoryt/skep`
2. no remote (local-only repo) → `<host>:<abs path>`

Two clones of `git@github.com:metheoryt/skep.git` share memory across hosts and
profiles — correct, they *are* the same repo. A local-only repo shares with nobody —
also correct. (`skep` itself was local-only through Plan 1; this case is real.)

`repo_key` is **derived on the worker and reported to the queen**, because the queen
has no filesystem access to the worker's repo (§2). It is not a display name; the
existing `repo` string stays for Telegram output.

---

## 7. Wire and protocol changes

### 7.1 New frames (reuse L0's req/reply layer, do not rebuild it)

`memory_write` / `memory_write_ack`, `memory_search` / `memory_search_reply`. These
ride the `req_id` + `dict[req_id, Future]` correlation layer built for the mailbox in
`ws_transport.py`. **Persist-before-ack**, as with `mailbox_send`. Link-down returns a
retryable error and never hangs — a wedged `await` in a spawned agent is worse than a
failed tool call.

### 7.2 `repo_key` is a protocol change, not a column

This ripples through L0/Plan-2 code and gets its own TDD task rather than being
smuggled into another. Enumerated touch points:

- `worker/` — compute `repo_key` at spawn (§6).
- `transport.py` — `EventSink.task_started`, `QueenInbox.on_task_started` signatures;
  `InMemoryEventSink`, `SwitchableEventSink`.
- `ws_transport.py` — `WsEventSink`.
- `wire.py` — the `task_started` frame.
- `queen/router.py` — pass through to bookkeeping.
- `queen/bookkeeping.py` — `ALTER TABLE entries ADD COLUMN repo_key TEXT` (nullable);
  `Entry.repo_key: str | None`; `add()` gains the parameter.

**Migration.** Existing rows get `repo_key = NULL`. Those tasks are historical and
terminal. A memory call from a task whose `repo_key` is `NULL` (impossible for a
newly-spawned task) is **rejected fail-closed** rather than falling back to the bare
repo name — a silent fallback would reintroduce exactly the collision §6 exists to
close.

---

## 8. Current value, history, and staleness

Append-only eliminates lost updates. It does **not** eliminate staleness, and the
content this spec scopes is precisely the time-varying kind: *"the stack takes 90s to
come up"* becomes false when it takes 30s. Under naive append-only that is a second
row, not a correction; FTS5 ranks by BM25 relevance rather than recency, so a stale
row can outrank the live one, and a fact written five times outranks a fact written
once. An agent would get contradictions with no signal for which is current.

**Decision: every write carries a `key`. The current value of a fact is the newest
row for `(scope, key)`. Superseding is just another append.**

- `key` is **required**: a kebab-case slug, validated, ≤ 128 chars
  (`compose-stack-startup-time`). Naming the fact is the discipline that makes
  supersede possible.
- `search` and `get` resolve to **current rows only**. Contradictions are unreachable
  through the agent-facing surface.
- Superseded rows are retained as **history** — the audit trail the future
  consolidation spec (§11) ranks and compacts, and the provenance the agent-comms
  survey named as its third primitive.
- Append-only is preserved end to end: **no row is ever updated or deleted in place.**

**Concurrent-write conflict resolution is a conscious YAGNI.** The survey's second
primitive (optimistic version-checking) exists to prevent lost updates; immutability
prevents them structurally. Two agents superseding the same key concurrently produce
two rows, and the newest wins — last-write-wins, but with the loser preserved rather
than destroyed. Revisit only if concurrent supersedes of the same key are observed.

Every row carries provenance: `author` (derived, §3.1), `task_ref`, `created_at`.

---

## 9. The two tools

Added to the **existing** per-agent FastMCP shim in `worker/mcp_shim.py` — same
ephemeral localhost port, same per-agent bearer token, `tid` closed over. No second
server.

```
memory_write(scope: "task"|"repo"|"profile", key: str, body: str, tags: list[str] = [])
    -> {ok, entry_id, superseded_id | None} | {ok: false, error}

memory_search(query: str, scope: str | None = None, limit: int = 10)
    -> [{scope, key, body, author, created_at, rank}]
```

**`memory_search` searches the union of every scope the calling task may read** —
`task:<ref>`, `repo:<repo_key>`, `profile:<profile>`, and `ceo` — because an agent
asking "what do I know about X" should not have to guess a namespace. Each hit carries
its `scope` so the agent knows the provenance and the blast radius of what it found.
The optional `scope` argument narrows to one kind. ACL fan-out is four instances
resolved from a single bookkeeping row; there is no enumeration and no scan.

`memory_write` rejects `scope="ceo"` (§5). The CEO writes that scope through Telegram.

### 9.1 FTS5 `MATCH` is a query language — this is the injection surface

`MATCH` does not take a literal string. Agent-supplied text containing `"`, `*`,
`NEAR`, `OR`, or `-` will crash the query or **silently change its meaning**. This
layer's one injection vector, and it gets its own test.

Query sanitization: split on whitespace, drop empty tokens, escape each token by
doubling embedded `"`, wrap each in `"…"`, join with a space (FTS5's implicit AND).
Operators are thereby data, never syntax. A query that sanitizes to zero tokens
returns an empty result rather than an error.

---

## 10. Assembly, caps, config

### 10.1 Both wiring paths, or the feature is inert

`queen/assembly.py` gains `build_memory_service`, called by **both** `build_queen`
(WS path) and the single-process `app.main`. `SwitchableMemoryClient` and
`InMemoryMemoryClient` mirror the mailbox pair.

This is a direct response to a real bug: L0's whole-branch review found the worker
assembly never wired the mailbox — the feature shipped **inert** — and L0.1 close-out
#4 found the single-process path built the switch but never set its target. An
assembly test asserting *both* paths is a required deliverable, not a nicety.

### 10.2 Caps (mirroring `MailboxService`)

| Knob | Default | Env |
|---|---|---|
| body cap | 16384 bytes | `SKEP_MEMORY_BODY_CAP` |
| key length | 128 chars | — |
| write rate limit | 20 / 60s per author | `SKEP_MEMORY_RATE_LIMIT` |
| search limit | 10 (max 50) | `SKEP_MEMORY_SEARCH_LIMIT` |
| db path | alongside `mailbox.db` | `SKEP_MEMORY_DB` |

Over-cap body, invalid key, and unknown scope are **rejected in the ack** (a
synchronous reply), not dead-lettered — unlike mailbox delivery, there is no
asynchronous recipient to fail toward.

`memory.db` is a **separate file** from `mailbox.db`: different lifecycle, different
retention, and a corrupt memory store must not take mail down with it.

---

## 11. Seams the consolidation spec (L1.5) will need

Recorded now so the substrate does not have to be reopened, and **not implemented**:

- `MemoryStore.history(scope, key)` — every row for a key, newest first. The store
  supports it; it is not exposed as an agent tool.
- Retained superseded rows (§8) are the corpus to rank and compact.
- `author`, `task_ref`, `created_at` on every row are the ranking signals.
- Search-miss logging (§4) is the evidence base for both L1.5 and the vec decision.

No `compact()` / `rank()` method is added speculatively. Designing a seam for a
consumer that does not exist is how seams end up wrong.

---

## 12. Testing strategy

Test-first, against the **in-memory seam** with an **injected clock** — no real
WebSocket, no real Telegram, matching L0. `fake_claude` cannot call MCP, so real
tool round-trips stay integration/manual; shim handlers and the seam's req/reply are
unit-tested directly.

Each group red→green:

1. **Store** — FTS5 startup assertion; BM25 ranking; append-only (no in-place update);
   `(scope, key)` current-value resolution; superseded rows retained; `history()`.
2. **Sanitization** — `"`, `*`, `NEAR`, `OR`, `-`, unicode, empty-after-sanitize.
   Asserts operators are treated as data.
3. **Scope resolution** — kind → instance from a bookkeeping row; fail-closed on
   unknown kind, missing ref, `NULL` repo_key; `ceo` write rejected.
4. **Isolation (the security tests)** — a task on profile `work` cannot read
   `profile:personal`; a task on repo A cannot read repo B; two repos with the same
   basename but different `repo_key` do not share a scope; two clones of one remote
   **do** share.
5. **Caps** — body, key length, rate limit, search limit clamping.
6. **Transport** — the four frames over the req/reply layer; persist-before-ack;
   link-down returns retryable, never hangs.
7. **Shim** — both tools; `tid` closed over; bearer enforced.
8. **Assembly** — memory reachable on **both** the WS path and the single-process
   path (regression guard for the inert-feature class of bug).

---

## 13. Open questions

- **Key discipline.** Agents choosing inconsistent keys (`stack-startup` vs
  `compose-startup-time`) fragment a fact instead of superseding it. Unsolvable in
  code; mitigated by the tool description and, later, by L1.5 consolidation. Watch it.
- **`ceo` scope partitioning** (§5.1) — global today. Revisit only on a concrete need.
- **Search-miss rate** is the trigger for the sqlite-vec decision (§4). Instrument it
  from day one; do not act on intuition.

---

## 14. Sources

- Anthropic has no first-party embeddings endpoint; Voyage AI is the recommended
  provider — `platform.claude.com/docs/en/build-with-claude/embeddings` (fetched
  2026-07-09).
- Namespace partitioning + per-partition ACLs, explicit concurrent-write conflict
  resolution, provenance/staleness — the three primitives from the 2026-07-05
  agent-comms prior-art survey (`.claude/memory/project.md`).
- L0 §12 (shared access path), §11 (spoof-proof `from`) —
  `2026-07-05-l0-mailbox-design.md`.
