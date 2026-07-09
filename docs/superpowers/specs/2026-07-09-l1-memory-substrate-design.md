# L1 — Agent Memory (design)

**Date:** 2026-07-09
**Status:** Implemented and merged, then **SUPERSEDED the same day** by
`2026-07-09-l1.1-agent-memory-files-design.md`. Its §1 decision ("agent memory is gortex
memory; skep stores nothing") no longer holds: memory is now tracked files under
`<repo>/.agent-memory/`, because a daemon-owned store cannot be inspected, diffed, or
reviewed. Most of this spec's plumbing survives — read L1.1 §7 for what was kept. The
rest of this document is retained for the reasoning and the verified facts in §2.
**Revision 2.** Revision 1 of this file specified a queen-hosted SQLite store with
four new WebSocket frames, a per-scope ACL matrix, and a repo-identity protocol
change across six modules. It was over-built for what the fleet needs today. That
design is not discarded — it is the **end state** (§7), deferred until agents actually
need to share memory across machines. This revision specifies what to build now.

**Relationship:** Second capability of the "skep as an autonomous agent company"
north star. Sits on **L0 Mailbox** (`2026-07-05-l0-mailbox-design.md`), built and merged.

---

## 1. Decision

**Agent memory is gortex memory. skep stores nothing.**

`gortex memory store | recall | surface` is already a durable, cross-session memory
store with everything this layer needs: per-repo and per-machine scopes, superseding
of stale facts, importance weighting, tags, provenance, and relevance-ranked recall.
It runs on every worker box already (mandated machine-wide in the user's global
`CLAUDE.md`). Building a second memory system beside it would be duplication.

skep's entire contribution is **one system-prompt addendum at spawn**, telling the
agent that memory exists, where it lives, and when to use it — plus a preflight check
so a missing daemon degrades quietly instead of handing the agent a command that fails.

Per-task scratch stays a plain file in the agent's worktree. No mechanism needed: the
agent has a worktree and a `Write` tool.

---

## 2. What was verified (2026-07-09, gortex v0.56.0)

Recorded because two plausible-sounding claims turned out false, and one turned out
true only with a flag.

| Claim | Verdict |
|---|---|
| Anthropic offers a first-party embeddings endpoint | **False.** Docs point to Voyage AI. This is what killed the sqlite-vec plan in revision 1. |
| gortex memory works from a skep agent's worktree | **False.** Agents run at `worktrees_root/<repo>-<tid>`; the daemon tracks only the parent repo. `gortex memory store` fails: *"the gortex daemon does not track …"* |
| …but works with `--index <parent repo>` | **True.** Verified store + recall from inside a live worktree; the memory lands in the parent's workspace (`project_id: skep`, `scope: workspace`). |
| Agents can reach gortex via the profile's **MCP** tools (spawn omits `--strict-mcp-config`) | **UNVERIFIED.** The MCP tools hit the same cwd-coverage gate, and it is unknown whether `store_memory` accepts a repo override. **This spec does not depend on it.** |

**Therefore the mechanism is the CLI, not MCP.** Workers are native (not
containerized), so `gortex` is on `PATH` and the agent reaches it through `Bash`. If
the MCP path is ever wanted, it needs its own empirical check — spawn a real agent in
a worktree and have it call the tool — before anything relies on it.

---

## 3. Mechanism

At spawn, `AgentProcess._argv` gains `--append-system-prompt <addendum>` (flag
confirmed present in `claude --help`, 2026-07-09). The addendum names the repo path
and is **prescriptive about when to write**, not merely what the commands are —
prescriptive trigger conditions measurably lift correct tool use:

```
## Memory

You have durable memory for this repo, shared with every agent that works on it.

Before starting, recall what is already known:
    gortex memory recall --index <repo_path> --limit 10

Store a memory when you learn something that would save the next agent time and
that the repo itself does not already record — an operational fact (this stack
takes 90s to come up; this test flakes under load), a constraint you discovered
the hard way, or a decision and its reasoning:
    gortex memory store --index <repo_path> --kind gotcha \
        --title "<short caption>" --body "<what + why>"

If a memory you find is now wrong, supersede it rather than adding a contradiction:
    gortex memory store --index <repo_path> --supersedes <id> --body "<corrected>"

Do NOT store what the repo already records — code structure, git history, CLAUDE.md.
Where the repo holds the fact, reference it instead of copying it.

Scratch notes for this task alone: write a file in your worktree.
```

`<repo_path>` is the worker's `repos_root/<repo>`, which `Supervisor.spawn` already
resolves before creating the worktree.

### 3.1 Graceful degradation (the dependency is soft)

**Memory is an enhancement, not critical path.** An agent with no memory still does
its task. So the dependency must never fail a spawn.

Preflight, once at worker startup (not per-spawn):

- daemon reachable and the repo tracked → include the addendum.
- daemon down, `gortex` absent from `PATH`, or the repo untracked → **omit the
  addendum entirely** and log a single visible warning naming the reason.

Omitting is what matters: never hand an agent a command that will fail. An agent told
to run `gortex memory recall` against an untracked repo burns turns on an error and
may conclude its memory is empty rather than unavailable.

`SKEP_MEMORY_ENABLED` (default true) forces it off.

---

## 4. Scopes — what gortex gives, and what it doesn't

| Need | Mechanism |
|---|---|
| Per-repo operational notes (the actual ask) | gortex `--scope workspace` (default), keyed by tracked repo |
| Cross-repo, host-wide knowledge | gortex `--scope global` |
| Per-task scratchpad | a file in the worktree; dies with it |
| CEO ↔ agent | **the L0 mailbox.** Already built. Not a memory scope. |

### 4.1 Profile isolation is an assumption, not a guarantee

Owner-settled: *global profile memory should be isolated between profiles.*

gortex has **no per-profile scope.** Its daemon is per-user-per-machine. Profile
isolation therefore holds only because the personal (`~/.claude`) and work
(`~/.claude-work`) profiles live on **separate WSL distros with separate daemons and
separate tracked-repo sets.**

**This is a documented assumption, and it is load-bearing.** Co-locate both profiles
on one host and they share a repo's workspace memory — a work agent's operational
notes become readable by a personal agent. That is precisely the cross-profile leak
revision 1's `repo_key` machinery existed to close. If profiles are ever co-located,
this decision must be revisited before that happens, not after.

Owner-settled and consistent with the above: *when two profiles work the same repo,
repo-related things are shared.* gortex's per-repo workspace scope gives exactly that,
for free, within a host.

---

## 5. Assumptions and risks

- **gortex daemon must run on each worker host** and track each repo agents work in.
  Mitigated by preflight (§3.1), not eliminated.
- **`--index` is a gortex CLI flag on a pre-1.0 tool (v0.56.0).** It can churn. The
  preflight smoke-checks the exact invocation the addendum will recommend, so a flag
  rename disables memory loudly rather than breaking agents silently.
- **An agent may simply ignore the instruction.** Unfixable in code; mitigated by the
  prescriptive phrasing in §3. Measure before assuming it works.
- **Key/topic discipline.** Agents may store near-duplicate memories rather than
  superseding. gortex's `--supersedes` is offered in the addendum; drift is a known
  cost of not having consolidation (§7).
- **Profile co-location leaks memory** (§4.1).

---

## 6. Testing

Small surface, so a small suite. No running daemon required — the preflight probe is
injected.

1. `_argv` includes `--append-system-prompt` with the correct `<repo_path>` when the
   probe reports memory available.
2. `_argv` omits it entirely when the probe reports the daemon down, `gortex` missing,
   or the repo untracked — and a warning is logged once.
3. `SKEP_MEMORY_ENABLED=false` omits it regardless of probe result.
4. Spawn succeeds in every unavailable case (the dependency is soft).
5. The addendum's recommended `gortex memory` invocation is the same string the
   preflight smoke-checks (guards against the two drifting apart).

---

## 7. End state (deferred, not discarded)

The 2026-07-05 decision stands: a **queen-hosted, centrally-governed memory substrate
that workers read and write through the queen.** It remains right, and L2's persistent
managers will need it — a manager's durable identity is state the queen persists.

It is deferred because its complexity buys exactly one thing this fleet does not yet
need: **sharing memory across machines.** Revision 1's most expensive component
(`repo_key` and its six-module protocol change) existed solely to make cross-profile
repo sharing leak-safe, and that case is rare today.

**Trigger to build it:** agents on different hosts, or co-located profiles, needing
shared memory. **Not** a trigger: the store feeling small.

Also deferred, and unchanged from revision 1's reasoning:

- **Consolidation / "sleep cycle."** It presupposes a populated store, and it *is* the
  experiment testing "agents improve with experience" — a hypothesis, not a
  foundation. Its own spec, once memory is accumulating and there is evidence it bloats.
- **Vectors.** No first-party embeddings endpoint (§2). gortex's own recall is
  sufficient until a search miss is observed.

**Nothing built here is thrown away.** The queen-hosted store, when built, ingests the
same notes; the addendum's phrasing survives; the preflight becomes a transport check.

---

## 8. Sources

- Anthropic has no first-party embeddings endpoint; Voyage AI is recommended —
  `platform.claude.com/docs/en/build-with-claude/embeddings` (fetched 2026-07-09).
- Worktree reachability, `--index` override, `claude --append-system-prompt` /
  `--add-dir` — verified locally 2026-07-09 (§2).
- Star topology; queen-hosted memory decision (2026-07-05); profile↔repo binding —
  `.claude/memory/project.md`.
- Prescriptive "call this when…" tool descriptions lift correct tool use — `claude-api`
  skill, tool-description guidance.
