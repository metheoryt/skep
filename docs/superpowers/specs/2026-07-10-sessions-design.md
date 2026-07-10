# Sessions — design

**Sub-project A of the multi-provider evolution.** Depends on nothing; B (runner
seam), C (fleet catalog), D (session spawning), and E (Telegram role) all attach to
the primitives defined here. This spec *defines* Sessions and *references* B/C/E by
their interface only — see §10.

Supersedes the "L2 persistent managers" framing in the north star: a manager is not
a session (see §2). Rewrites `ARCHITECTURE.md` §7 on merge.

---

## 1. The problem

Today skep's unit of work is a **task**: one `claude -p` process, one worktree, one
Telegram topic, and the topic dies when the process ends. That was right for
fire-and-forget spawns. It cannot express what the fleet now needs:

- A unit of work that **outlives a single process** — parked at a usage limit and
  resumed later, or tuned to a different model between turns.
- A unit of work an agent can **create anywhere in the fleet**, not just the CEO.
- A unit of work spanning **more than one project directory**.
- A durable **identity** (a manager) that is addressed, accrues memory, and is
  rehydrated on demand — distinct from any one execution.

This spec introduces three concepts that separate what "task" was overloading.

---

## 2. Three concepts

**Session** — a pinned execution context. Fixes, at creation and for its whole life:
one host, one profile, one runner (e.g. Claude Code), one workspace, and the set of
worktrees it operates in. Owns a Telegram topic. Is addressable by a fleet-global
`ref`. Can be parked and resumed. Can spawn child sessions. **The model is *not*
pinned** — it lives on the Invocation.

**Invocation** — one run of a runner inside a session. Holds the `resume_token`
(the runner's own resume handle) and the `model` for that run. A session parked and
resumed has two invocations; a session whose model is retuned starts a new one.

**Manager** — a durable identity *above* sessions: a role, a system prompt, a memory
scope, and an inbox address (`mgr:<name>`). **Not a session and not a process.** Its
continuity is memory (L1.1 `.agent-memory/`) plus mailbox (L0), not a transcript.
When a manager acts, it is *rehydrated into a fresh session* that reads its memory
and inbox, acts, and ends. Specified in a later sub-project; named here only so the
schema and topic model leave room for it (§6, §7).

> **Why manager ≠ session.** A session's continuity is a *transcript* —
> provider-native, host-bound, resumed by `resume_token`. A manager's continuity is
> *memory + inbox* — it wakes fresh, never replaying a conversation. Different
> continuity mechanism ⇒ different primitive. This is also why skep never owns a
> transcript (§9): every session resumes through its runner's own mechanism, or has
> none and restarts.

### The naming collision this resolves

`Registry.tasks.session_id` today holds Claude Code's per-run resume handle,
harvested from the runner's `system` event. That is an **Invocation's
`resume_token`**, not a Session. The column is renamed `resume_token` in this spec
(internal only; no external surface). "Session" is the new higher-level concept and
must never share that name.

---

## 3. Ownership

| Concern | Owner | Backing |
|---|---|---|
| Session identity (`ref`, host, profile, workspace, topic, status) | **queen** | promotes the existing `Bookkeeping` `ref` |
| Invocation (worktree, pid, `resume_token`, model, per-run status) | **worker** | the existing `Registry.tasks` row, keyed by session `ref` |

This split already exists — `Bookkeeping.entries` maps a global `ref` to a worker's
`local_id`. Sessions rename and extend it; they do not invent it. The queen never
learns a `resume_token` (it is provider- and host-specific worker state).

---

## 4. Lifecycle

```
created ─▶ running ─┬─▶ done
                    ├─▶ failed
                    ├─▶ killed
                    └─▶ parked(until) ─▶ running   (new invocation, same worktree)
```

- **Parking** requires the runner to declare `supports_resume` (B's interface), and
  requires the **same host and same worktree** — both already true of Claude Code,
  both already recorded in the park-&-resume decision (`resume_token` lookup is
  scoped to the working dir + its worktrees). A runner without resume cannot park; a
  usage-limit hit there is a `failed`, surfaced as such.
- **Resuming** is a new Invocation on the same worktree with the stored
  `resume_token`, optionally with a different `model` (§5).
- A **parked session keeps its topic** — that is where "parked until 3:45pm" is
  shown and where the resume streams back.

---

## 5. Model per Invocation

`model` lives on the Invocation, not the Session. A new invocation (spawn, unpark,
retune) may specify a different model on the same session.

**Verified (probe, 2026-07-10, `claude` v2.1.206):** `claude --resume <id> --model X`
runs the next turn on X (checked on the `assistant` stream-json events). Resuming
with no `--model` defaults to the last-used model. So a manager can tune a child
session's model between turns without touching the session's pin.

**Boundary:** model varies *within a provider*. Provider never varies within a
session — crossing providers would require skep to own and translate a transcript,
which §9 rules out.

**v1 scope:** model changes only at invocation boundaries (spawn / unpark / next
turn). Live mid-run preemption (kill + resume-with-new-model) is a later control
operation; the schema supports it, v1 does not build it.

---

## 6. Workspace — an ordered list of roots

A session's workspace is **one or more roots**, not a single folder. Mechanism:
the runner takes the first root as `cwd` and the rest as `--add-dir` roots (Claude
Code); the `resume_token` stays valid because `cwd` is still exactly one folder —
`root[0]`.

Workspaces are **named and owner-defined** (queen-held, e.g. `workspaces.yaml`);
sessions and agents reference them **by name, never by path**. Name→path resolution
and the rule that a workspace is schedulable on a host only if that host provisions
all its roots belong to **C (fleet catalog)**; this spec assumes a resolved list of
local roots.

> **Security.** Root selection is privileged precisely because `--add-dir` is an
> arbitrary-filesystem-read primitive: a session that could name `~/.ssh` or another
> profile's config as a root defeats profile isolation and the star topology.
> Named-only references keep agents from ever naming a path. The `~/.ssh` escalation
> is the reason this is not ad-hoc.

### Per-root mode and access

Each root carries a **mode** (how the session relates to the directory) and an
**access** level (orthogonal):

| mode | meaning | lease |
|---|---|---|
| `new` | create and own a fresh worktree (today's behavior; the default) | none — you own it |
| `attach:<ref>` | join a worktree another session marked shared; opt-in both sides | none |
| `primary` | operate in the repo's main checkout | see access |

| access | meaning |
|---|---|
| `rw` | may write |
| `ro` | read-only (advisory — see risks) |

**Lease rule:** an exclusive, queen-held lease is required *exactly* when a root is
opened **`rw` by a session that does not own it** — i.e. `primary:rw`. `primary:ro`
needs no lease (readers coexist with each other and with the writer). One
`primary:rw` lease per `(host, repo)`; released on park or end or worker disconnect;
**not inherited** by spawned children.

**Branch operations** (`checkout`, `reset`, `stash`, `rebase` — anything that
mutates the index/`HEAD` globally) are **forbidden in a shared or primary root** by
declaration in the spawn addendum. Only the sole `rw` owner of a worktree may switch
branches. (Advisory; see risks.)

**The canonical pattern:** `root[0]` = the session's own worktree (`new`, `rw`);
the primary checkout added as a secondary root (`primary`, `ro`) — the agent works
in isolation while *watching the tree as it exists in the CEO's open IDE, including
uncommitted changes*. A detached read-only worktree snapshot cannot show
uncommitted work; attaching read-only to the primary checkout is the only way.

**Not in scope:** machine-resource exclusion (a bound port, a Compose stack, a
shared DB) that blocks parallel test runs. That is a named-resource lease, a
different feature; recorded as a recognized need, built later — otherwise Sessions
grows a resource scheduler.

---

## 7. Topic ↔ Session

`Bookkeeping` moves from topic-per-**task** to topic-per-**session**. The topic
lives as long as the session (including while parked), not the process. Existing
rows migrate as one-invocation sessions.

**Managers** get a persistent topic each (owner decision): a durable place to
address a manager and receive its reports. Their spawned sessions get child topics
or stay hidden per visibility (§8).

**Telegram-driven lifecycle (references E).** Closing a topic from Telegram *parks*
its session if the runner supports resume (else refused with a message); reopening
*resumes*; closing an already-parked session just archives. Whether the bot observes
`forum_topic_closed`/`forum_topic_reopened`, and the per-chat edit-rate ceiling for
long-lived streaming sessions, are **E's probes** — this spec assumes the binding is
bidirectional and flags the ceiling as a risk.

---

## 8. Visibility

`visible: bool` on the session; `spawn_visibility: ask | auto | hidden` governing
children, **inherited from the parent** unless overridden per-agent. A hidden session
gets no topic but is still recorded and still addressable by `ref`.

**Rule: a hidden session that `failed` surfaces anyway** — to the parent's topic, or
to the CEO if the parent is also hidden. Silent failure in an invisible child is how
a day is lost.

---

## 9. Non-goals (recorded to stop re-litigation)

- **skep never owns or replays a transcript.** Sessions resume through their runner's
  native mechanism, or restart. Cross-provider transcript translation is out forever.
- **Sessions never migrate across providers or hosts.** The fleet is spanned by the
  **child-session tree** (D), not by a session teleporting. Rationale also empirical:
  usage limits are account-scoped, so moving a parked session to another host does
  not dodge the limit that parked it.
- **The queen never runs an LLM.** It is the trust anchor (bot token, shared secret,
  mailbox DB); an LLM inside it puts prompt injection at the one component every node
  trusts. Intelligence lives in agents — promote one to analyst instead.

---

## 10. Seam interfaces to the other sub-projects

This spec depends on these and specifies none of them:

- **B (runner seam):** a runner declares a capability manifest; the only field
  Sessions reads is `supports_resume: bool`. Model-per-invocation additionally
  assumes `model` is a per-run runner argument.
- **C (fleet catalog):** resolves a workspace **name** to a list of local roots on a
  given host, and answers whether a host can schedule a given workspace.
- **E (Telegram role):** owns the edit-rate budget and the topic close/reopen
  service-message semantics that §7 drives session lifecycle from.
- **D (session spawning):** the `spawn_session` MCP tool that lets an agent create a
  child session; consumes this spec's Session primitive, workspace names, and
  visibility rules.

---

## 11. What changes in shipped code

- `Registry.tasks.session_id` → `resume_token` (internal rename).
- `Registry.tasks` gains `model`; the row is an **Invocation**, keyed by session
  `ref`; a session may have several.
- `Bookkeeping.entries` becomes session-scoped; topic lifetime follows the session.
  Existing rows migrate as one-invocation sessions.
- `Supervisor.spawn` accepts a resolved workspace (list of roots + per-root
  mode/access) instead of a single `repo`; renders `cwd` + `--add-dir`.
- The queen gains: a session registry (extends `Bookkeeping`), the `primary:rw`
  lease table, and the workspace-name store (the store itself is C's).
- `memory_shim`'s `remember` gains a `project` argument (which root's
  `.agent-memory/` receives the write), defaulting to `root[0]`. Reading unions all
  roots' stores.

---

## 12. Risks

- **`ro` and shared-root no-branch-switch are advisory, not enforced.** Agents have
  `Bash`; skep declares the constraint in the spawn addendum to a cooperative agent.
  Real enforcement is Phase 4 (sandbox). Stated plainly so no one mistakes the
  declaration for a sandbox.
- **A lockless `ro` reader can observe a torn tree** — if the `rw` owner (or the CEO)
  runs `git checkout`/rebase mid-read, the reader sees a half-switched directory. Not
  corruption, but possibly wrong answers.
- **Long-lived streaming sessions may hit the Telegram per-chat edit-rate ceiling**
  (E's probe). Topic-per-session viability depends on it; the live-edited activity
  message may need coalescing.
- **Migration of shipped `Bookkeeping`/`Registry` schemas.** Both carry data; the
  one-invocation-session backfill must be verified against existing rows.
- **Manager topics accumulate.** A durable topic per manager needs an archive policy
  (E) or the control group fills with dormant managers.

---

## 13. Testing

- Session lifecycle as a pure state machine over the queen's registry (created →
  running → parked → running → done), no runner needed.
- Invocation ↔ session keying: one session, N invocations, correct `resume_token`
  and `model` per invocation.
- Workspace rendering: roots → `cwd` + `--add-dir`; per-root mode/access → lease
  requirement (only `primary:rw` demands a lease).
- Lease: acquire/release on park/end/disconnect; second `primary:rw` on the same
  `(host, repo)` refused; children do not inherit.
- Visibility inheritance, and the hidden-`failed`-surfaces rule.
- `remember` targeting: write lands in `root[0]` by default, in the named root when
  given; read unions.
- Migration: existing `Bookkeeping`/`Registry` rows become valid one-invocation
  sessions.
- Model-per-invocation rests on the 2026-07-10 probe; re-run it (§5) if the `claude`
  CLI version moves, in the L1.1-§8.1 spirit of not marking unrun claims proven.
