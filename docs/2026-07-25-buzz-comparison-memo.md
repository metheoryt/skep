# Buzz (Block) vs skep — comparison memo

**Written 2026-07-25.** Buzz was announced 2026-07-21. This is a research memo, not
a design and not a plan: it records what Buzz is, where its seam falls relative to
skep's, what is worth borrowing, and what would decide whether skep continues.
It is a snapshot of Buzz at that date and will rot; re-verify before acting on it.

---

## 1. What Buzz is

Buzz is a **self-hostable collaboration substrate where humans and agents are the
same kind of participant**. It is a Nostr relay: every message, reaction, workflow
step, review approval, and git event is a signed event in one log, with the same
shape and the same audit trail whether the author is a person or a process. Block
open-sourced it under Apache-2.0 (`github.com/block/buzz`) with a hosted option at
buzz.xyz.

**Stack.** Rust workspace (~26 crates). Relay is Axum over WebSocket + REST;
Postgres holds events and full-text search; Redis for pub/sub, presence, typing;
S3/MinIO for media via Blossom. Desktop client is Tauri + React, mobile is Flutter
(beta). Nostr NIPs in use: NIP-01 (events/subscriptions), NIP-42 (auth, Schnorr),
NIP-34 (git patches, repo announcements, status), NIP-98 (HTTP auth).

**Identity is the load-bearing idea.** Every participant — human or agent — holds
its own keypair. There is no role/permission matrix; access is scoped by identity
and channel membership, and the signed event log *is* the audit trail. An agent's
key belongs to the agent, not to the platform, so its identity is portable across
relays.

**Agents are room members, not bots.** They open channels, read and write canvases
and media, react, run YAML workflows (message / reaction / schedule / webhook
triggers), approve gates, search six months of history, and orchestrate other
agents. Repos are first-class: patches land as NIP-34 events, and a git hosting
backend ships today.

**How agents actually run.** Three crates, and the distinction matters:

- `buzz-acp` — a harness that wires external coding agents in over the **Agent
  Client Protocol**. It listens for `@mentions` on the relay, spawns the agent over
  stdio, and the agent replies through `buzz-cli`. Listed as working today. Only
  **Goose** speaks ACP natively; **Codex** goes through the third-party
  `@agentclientprotocol/codex-acp` npm adapter, and **Claude Code** through
  `@agentclientprotocol/claude-agent-acp`, which wraps the **Claude Agent SDK** (not
  the `claude` CLI) and is documented as needing `ANTHROPIC_API_KEY`. §5 depends on
  this detail.
- `buzz-agent` — Block's own ACP agent: an LLM loop over stdio JSON-RPC, up to
  **8 concurrent sessions** in one process, each with its own MCP servers, history
  and context; self-summarizes when context fills. Works behind any ACP client
  (Zed, JetBrains, buzz-acp).
- `buzz-dev-mcp` — an MCP server giving any agent a shell and a file editor.
  Ephemeral processes, process-group kill on every exit path, bounded output, edits
  resolved against the working directory.

**Honest status** (their own table): working — relay, channels, threads, DMs,
canvases, media, search, audit log, desktop app, `buzz-cli`, ACP harness, YAML
workflows, NIP-34 git events, git hosting backend. Being wired up — mobile clients,
workflow approval gates, huddle lifecycle. Not built — web-of-trust reputation,
push notifications. Also: **one authoritative relay, no replication and no
peer-to-peer today**, despite the protocol allowing it.

---

## 2. The seam falls in a different place than skep's

The instinct is to read Buzz as a competitor to skep. It mostly is not — the two
draw their boundary at different heights.

- **Buzz is a collaboration and identity substrate.** Its centre of gravity is the
  event log: who said what, signed, searchable, with git and workflow events in the
  same stream. Its execution story (`buzz-agent`/`buzz-dev-mcp`) is deliberately
  minimal — "small enough to read in an afternoon" — and it has no notion of *which
  machine* an agent runs on, *which credential profile* it uses, or *which
  checkout* it works in.
- **skep is an execution control plane.** Its centre of gravity is spawning and
  supervising `claude -p` processes on named `(host, profile)` pairs: worktree per
  task, per-profile credential isolation, env default-drop, capacity caps, MCP token
  off argv, session↔invocation identity, usage-limit park and auto-resume.

Where they touch is precisely **skep's queen/Telegram half**, not its worker half:

| skep piece | Buzz counterpart |
|---|---|
| `telegram_gw` + `telegram_sink`, one topic per task | channels, threads, DMs, with real desktop/mobile clients |
| `Bookkeeping` (ref → topic id, message id) | **evaporates** — it exists only because the Telegram Bot API cannot read topics back |
| `Mailbox` + `addressing` (`ceo`, `mgr:<name>`, IC refs) | signed events in the shared log; DMs and channels |
| `auth.py` HMAC on one shared `SKEP_SHARED_SECRET`; `SKEP_OWNER_ID` gate | per-participant keypairs, NIP-42; scoping by identity |
| worktree isolation, profile isolation, env hygiene, capacity, park/resume, session↔invocation | **no counterpart** |
| L2–L5 (persistent managers, delegation, earned autonomy, mentorship) | none built; web-of-trust reputation is in their "pending code" column |

That table is most of the answer to "do we continue skep": everything skep has
shipped in the last three weeks — Sessions A1–A3, L0.2 — is in the row Buzz does
not touch. What Buzz would replace is the part of skep that was always the
cheapest and most obviously provisional: Telegram as the control surface, plus the
bookkeeping that exists solely to work around Telegram's API.

There is one genuine overlap worth naming: `buzz-agent`'s 8 concurrent sessions
against skep's `max_concurrent` default of 8. But it is concurrency *within one
process on one host*, with no worktree binding and no credential separation —
closer to skep's `Supervisor` than to skep's fleet.

---

## 3. What Buzz has that skep does not

1. **Per-participant cryptographic identity.** skep has one shared HMAC secret
   between queen and workers plus a single-owner Telegram gate. Every agent is
   anonymous inside that trust boundary. Buzz gives each agent its own key, so
   "who did this" is answerable and unforgeable.
2. **A signed, immutable action log.** skep has a `Registry` audit table and
   Telegram scrollback. Buzz makes the log the substrate — the same events humans
   read are the audit record.
3. **Multi-participant workspace.** skep is single-operator by construction
   (`SKEP_OWNER_ID`). Buzz is a team product with real clients on desktop and
   mobile.
4. **Git as first-class events.** NIP-34 patches, repo announcements and status in
   the same stream as the discussion, plus a hosting backend.
5. **ACP as a runner seam that already exists.** Claude Code, Codex and Goose behind
   one protocol.
6. **YAML workflows with triggers and approval gates.** skep's Phase 3 ("talk-back
   and brakes", `ask_human`, gated ops) is unbuilt; this is the same idea shipped.
7. **A UI that is not a chat bot.** Canvases, media with frame-anchored comments,
   search over history.

## 4. What skep has that Buzz does not

1. **Fleet dispatch across machines.** `/spawn <host> <profile> <repo> <task>` —
   Buzz has no host concept at all.
2. **Profile isolation.** One worker per Claude config dir, so a personal agent
   cannot read work credentials. This is the reason multiple workers exist, and
   Buzz has no analogue.
3. **Git worktree per task.** Buzz's `buzz-dev-mcp` resolves edits against a
   working directory; nothing creates or reaps an isolated checkout per task.
4. **Env hygiene / default-drop allowlist and token-off-argv.** L0.2 Increment 1.
5. **Usage-limit park and auto-resume** (Sessions A3) — detect the limit, park the
   session live-but-idle with its topic and worktree intact, sweep, resume the same
   session in the same topic. Nothing in Buzz addresses provider rate limits.
6. **Session vs invocation as a modelled distinction**, with resume tokens and
   ref/topic reuse across invocations.
7. **The org ladder** — L2 persistent managers, L3 delegation, L4 earned autonomy,
   L5 mentorship. Buzz's closest item (web-of-trust reputation) is explicitly
   unbuilt.
8. **Zero infrastructure.** `uv run skep`. Buzz needs Postgres + Redis + S3/MinIO +
   the relay, and a desktop client to be pleasant.

---

## 5. What to borrow, ranked by value over cost

**1. ACP as the *shape* of the runner seam — but not as a replacement for the
`claude` path.** Sessions sub-project B is "runner seam, unstarted", and ACP is the
strongest existing candidate for what that seam should look like: one stdio
JSON-RPC protocol, one client, N providers, no bespoke adapter each time. Buzz
proves the client side is small — `buzz-agent` and `buzz-acp` are both readable in
a sitting — and adopting the protocol costs no relay, no Nostr, no Rust.

The caveat is decisive and was found by checking rather than assuming. **Claude Code
does not speak ACP natively.** Buzz reaches it through
`@agentclientprotocol/claude-agent-acp`, a third-party npm adapter over the *Claude
Agent SDK*, documented with `ANTHROPIC_API_KEY`. Routing skep's Claude path through
that would trade away three things skep currently depends on: subscription
credentials rather than metered API billing; `CLAUDE_CONFIG_DIR`-based profile
isolation, which is the entire reason multiple workers exist; and the
`stream-json` event stream that `stream.parse_event`, the activity line and
`detect_usage_limit` are all built on. It would also put a third-party npm package
on the critical path of every spawn.

So the borrow is narrower than it first looks, and it is a design constraint rather
than a migration: **shape sub-project B so ACP is one implementation of the seam,
not the seam itself.** Keep the native `claude -p` runner as the first
implementation; make ACP the second, and let it earn Codex and Goose without
touching the Claude path. Reading `buzz-agent` and `buzz-acp` before writing B's
design is still worth the hour — they are the closest thing to a reference
implementation of the seam skep needs.

**2. Per-agent cryptographic identity and a signed action log.** Not necessarily
Nostr — the borrowable idea is that each agent gets a keypair and every action it
takes is signed under it, so authorship survives leaving the process that made it.
This is the missing substrate under L4: "earned autonomy and reputation" needs a
tamper-evident track record, and today skep has nothing to hang one on. Medium cost,
and it can be built incrementally against the existing mailbox and registry.

**3. Agents as durable room members instead of ephemeral per-task topics.** skep's
current shape is one topic per task, deleted on completion; identity dies with the
process. The north star's org hierarchy wants the opposite — a manager with a
durable name, inbox and history (that is exactly L2). Buzz's model, where the agent
joins channels the way a person does, is the better target shape, and adopting it
conceptually does not require adopting Buzz.

**4. Workflow triggers and approval gates as data.** YAML rules on message /
reaction / schedule / webhook, with a human 👍 as an approval event, is a
concrete and cheap design for skep's unbuilt Phase 3 brakes.

**5. Buzz wholesale as skep's front end (lowest priority, highest cost).** It would
delete `telegram_gw`, `telegram_sink`, `Bookkeeping`, most of `addressing`, and
arguably `mailbox`, replacing them with a relay client — a real simplification of
that half. The cost is Postgres + Redis + S3/MinIO + relay + a desktop client
against today's `uv run skep`, plus a dependency on a four-day-old project with a
single non-replicated relay. Only worth it if the answer to §6 is "multi-participant".

Not worth borrowing: Nostr itself, if the deployment stays single-operator — the
protocol's value is federation and portable identity across hosts, and neither
applies to one person's fleet.

---

## 6. Does skep continue?

The question turns on one thing that cannot be read off the repo:

> Is the goal a **multi-participant shared workspace** with audit and portable
> identity, or a **private single-operator fleet driver**?

If it is the workspace, Buzz has done years of work skep has not started — clients,
search, media, git events, identity — and rebuilding it is a bad trade. The right
move then is not to abandon skep but to **invert it**: keep the worker half as the
execution plane, drop the Telegram half, and connect the worker to a Buzz relay as
an agent identity.

If it is the fleet driver — which is what skep has actually been built as, and what
the single-owner gate, profile isolation and Telegram control surface all encode —
then Buzz does not replace it, because Buzz has no answer to "run this on that
machine, under that credential profile, in a fresh worktree, and park it when the
limit hits".

**Recommendation: continue skep, with two adjustments.**

- Treat the **execution plane as the asset** — worktree and profile isolation, env
  hygiene, capacity, sessions and invocations, park/auto-resume. Nothing surveyed
  here duplicates it, and it is where all recent work went.
- Treat the **Telegram layer as explicitly provisional**. It was never the
  interesting part; Buzz demonstrates what replaces it. Do not invest further in
  `Bookkeeping`-shaped workarounds for the Bot API. Any new control-surface work
  should assume the surface may change.
- Do **Sessions B (the runner seam) next**, before further Telegram-side work, and
  design it so ACP can be one implementation behind it (§5.1) — *not* by porting the
  Claude path onto ACP, which would cost subscription auth and profile isolation.

Buzz also usefully constrains skep's ambition: the L2–L5 ladder is a large,
unbuilt bet on agent organizations. Buzz's own reputation/web-of-trust work sits in
their "strong opinions, pending code" column — a company with a full team has not
built it either. That is not a reason to abandon the ladder, but it is a reason to
keep the execution plane, which is real and working, from being starved for it.

One residual, tracked elsewhere and not part of this memo: README still documents
the superseded L1 gortex memory, `detect_usage_limit` remains unverified against a
real usage-limit payload, and the park sweep has no give-up counter.

---

## 7. Sources and verification notes

- [Block — Introducing Buzz](https://block.xyz/inside/introducing-buzz-where-humans-and-agents-work-together) — announcement.
- [github.com/block/buzz](https://github.com/block/buzz) — `README.md`,
  `VISION_AGENT.md` and `crates/buzz-acp/README.md` read as raw markdown; crate list
  read from the GitHub contents API. The stack, NIPs, crate names, the status table,
  the 8-session cap in `buzz-agent`, and the per-provider ACP adapter chain all come
  from those primary files.
- [SiliconANGLE](https://siliconangle.com/2026/07/21/block-launches-buzz-open-source-workspace-humans-ai-agents/) ·
  [TFTC](https://www.tftc.io/buzz-block-nostr-ai-agent-workspace-launch) ·
  [Winbuzzer](https://winbuzzer.com/2026/07/24/block-launches-buzz-to-unite-team-chat-ai-agents-and-git-xcxwbn/) —
  corroborate the single-relay-no-replication point and the ACP integration.
- skep side read from `ARCHITECTURE.md` at `1895d71` (branch `metheoryt/buzz`) and
  `.claude/memory/project.md`.
- Not verified: nothing in Buzz was run or built locally. Every claim about Buzz is
  from its own documentation, which is four days old and self-reported.
