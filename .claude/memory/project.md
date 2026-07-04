# fleetd — project memory

<!-- Repo-local, git-tracked, auto-loaded at session start. Durable project facts
only (decisions, gotchas, constraints). One bullet per fact. No secrets. -->

## Decisions

- **Build-vs-buy (evaluated 2026-07-04): decided to BUILD fleetd despite heavy
  overlap with two official Anthropic features.** `claude remote-control --spawn
  worktree --capacity N` (worktree-isolated agent fleet driven from claude.ai /
  Claude mobile app, outbound-only HTTPS, `--sandbox`) covers much of fleetd's
  core; the official **Channels** Telegram plugin (`/plugin install
  telegram@claude-plugins-official`, pairing + allowlist auth) covers the
  Telegram+owner-lock piece but only pushes into ONE running session (no
  spawn/fleet/worktree/kill, no tool-by-tool streaming). Owner is on **Pro/Max
  claude.ai OAuth**, so Remote Control IS available to them — they chose to build
  anyway. **Differentiator that justifies fleetd:** Telegram-native *fleet*
  control (`/spawn`→worktree, `/ls`/`/kill`/`/panic` across many tasks) + live
  tool-by-tool streaming into per-task forum topics + fully self-hosted. Note:
  Remote Control is UNAVAILABLE on API-key/Bedrock/Vertex or a custom
  `ANTHROPIC_BASE_URL` — relevant if auth ever changes. **REVISITED 2026-07-04
  (confirmed BUILD):** Remote Control does cover talk-back/steering/sandbox/fleet
  but ONLY from claude.ai / the Claude mobile app, never Telegram; Channels is
  single-session (no fleet, no streaming). The unserved gap — Telegram-native
  *fleet* control + live streaming + self-hosted — is real. Continue building.

- **Phase 2 was RE-SCOPED 2026-07-04 from "talk-back + brakes" to "Queen +
  isolated workers"** (topology first, because talk-back routing depends on where
  the Telegram front lives). See `2026-07-04-fleetd-phase2-queen-workers-design.md`.
  Key settled decisions:
  - **Queen + workers are SEPARATE scripts/processes** (`fleetqueen`, `fleetd`),
    explicit roles — NO leader election / auto-failover / consensus. The bot
    token-as-lock `409` check is only a startup guard against an accidental
    double-queen.
  - **Bot token lives ONLY on the queen.** Workers hold only the WS shared secret
    (smaller blast radius). Queen owns all Telegram I/O + formatting
    (`telegram_gw.py`/`formatting.py` move to the queen); workers emit domain
    events over a WebSocket via an `EventSink`/`CommandSource` seam.
  - **Queen is containerized on the homeserver behind Caddy** (`fleet.cyphy.kz`
    → `reverse_proxy 10.0.0.2:8765`, WS upgrade is transparent); **workers stay
    native** (agents need host access). Queen never spawns agents, so it
    containerizes cleanly.
  - **Discovery: 3 tiers, same shared-secret auth** — mDNS (`_fleetd-queen._tcp`)
    on the LAN, `--queen-url wss://fleet.cyphy.kz` over internet, WG direct
    (`10.0.0.2:8765`). Bot API can't list topics → queen keeps a small
    bookkeeping SQLite `ref→(host,profile,local_task_id,topic_id,msg_id)`.
  - **Multi-worker isolation is first-class:** one worker per Claude profile,
    scoped by injecting `CLAUDE_CONFIG_DIR` (`~/.claude` personal vs
    `~/.claude-work` work) + a clean env into each agent. **`host` and `profile`
    are SEPARATE fields** (never a combined/parsed `host_id` string) carried
    through config, protocol, routing, bookkeeping. By-id commands use the queen's
    opaque global `ref` (`/kill 42`); `/spawn <host> [--profile p] <repo> <task>`.
    Each worker has its own repos_root/worktrees_root/registry.
  - Parallelism is intra-worker (Supervisor already runs N concurrent
    `AgentProcess`); multiple workers are for ISOLATION, not throughput. Per-worker
    `max_concurrent` capacity cap (default 8).
  - **Star topology: NO worker↔worker comms** — workers connect only to the
    queen; cross-worker interaction (if ever) routes through the queen. Worker
    trusts exactly one peer.
  - **Transport security:** worker↔queen is a fleet-wide RCE surface (a `spawn`
    cmd = code execution on the worker). Encrypted per hop (wss via Caddy /
    WG tunnel; plain-LAN ws is the weak hop). **Mutual challenge-response auth
    (HMAC over nonces), NOT a bearer token** — defeats mDNS-spoofed rogue-queen
    (the scary RCE vector) and worker impersonation; nonces stop replay.
  - New phasing: **P2 = queen/workers · P3 = talk-back + brakes · P4 = sandbox**.

- **Telegram Bot API 2026 updates make cross-fleet federation + bot provisioning
  NATIVE (Later scope, not P2).** Verified 2026-07-04 against core.telegram.org.
  Corrects two earlier WRONG assumptions in this project's discussion:
  - **Bot-to-Bot Communication (Bot API 10.0, 2026-05-08):** two bots can message
    each other by `@username` if BOTH enable "Bot-to-Bot Communication Mode" in
    @BotFather (private/groups/business). So autonomous fleet↔fleet federation
    ("like email", incl. an unreachable local-queen fleet) IS possible with NO
    human relay / NO MTProto userbot (earlier claim that bot-to-bot is impossible
    was wrong). Dev MUST implement loop-prevention (dedupe, rate-limit, depth cap).
  - **Managed Bots (Bot API 9.6, 2026-04-03):** a manager bot can create/configure
    bots for owners and fetch tokens via `getManagedBotToken` /
    `replaceManagedBotToken` (`ManagedBotUpdated`). This is the "our own BotFather"
    the owner asked about — viable now (earlier claim "only MTProto user-account
    can mint bots" was wrong). Provisions per-FLEET queen bots, not per-worker.
  - **DISCIPLINE:** these do NOT replace the intra-fleet WS transport. Routing
    workers via bot-to-bot would force a token onto every worker + Telegram rate
    limits + loop-prevention. WS stays for worker↔queen; bot-to-bot is the
    cross-fleet edge only. `aiogram` may not expose 9.6/10.0 yet — verify first.
    Sources in the Phase-2 design doc §16.
  - **Managed Bots does NOT obviate the control plane** (evaluated): the daemon
    must run on each host regardless (Telegram features don't execute code on your
    machines); WS stays intra-fleet. Only exception recorded: a remote low-traffic
    worker MAY use a bot-to-bot uplink instead of a public WS endpoint (§12.1).
  - **Queen group auto-onboarding (Plan 2, spec §10.1):** queen can discover groups
    via `my_chat_member`, self-register commands (`setMyCommands`+`BotCommandScopeChat`),
    check readiness (`getChat.is_forum`/`getChatMember`) + prompt — but CANNOT
    enable Topics (no Bot API method) or self-promote to admin (human grants both).
    Gate on owner-presence/allowlist. Makes `group_chat_id` optional; one fleet →
    one control group (multi-group streaming = YAGNI).

- **Phase 2 planning done (2026-07-04):** design approved; split into Plan 1
  (`plans/…-phase2-plan1-queen-worker-seam.md` — queen/worker split behind the
  in-memory transport seam, 9 TDD tasks, NOT yet executed) + Plan 2 (WebSocket +
  entrypoints + mutual auth + mDNS + heartbeat/presence + queen auto-onboarding +
  deploy — NOT yet written). Next step: execute Plan 1.

- **Agent-comms prior-art survey (deep-research, 2026-07-05): confirms the
  worker↔queen TRANSPORT is a BUILD, and surfaces shared vector memory as the
  strongest BUY/borrow signal.** 108-agent fan-out, 25 sources, 22 claims
  confirmed / 3 refuted via adversarial verification. Load-bearing findings:
  - **Transport = BUILD (confirmed).** No emerging standard targets a self-hosted
    WebSocket star. MCP / A2A / ACP are ALL HTTP-family (MCP=Streamable HTTP+stdio;
    A2A=JSON-RPC/gRPC/SSE; ACP=REST) — none uses WebSocket (only an unofficial WS
    dispatcher in a 3rd-party `a2a-rust` SDK). A2A specifically assumes
    peer-to-peer/mesh, which our star topology (no worker↔worker) forbids.
    Vindicates the existing WS + mutual-auth decision.
  - **Borrow addressing, don't invent it:** AutoGen Core is the canonical
    "message-bus for agents" — direct-messaging by agent ID + broadcast pub/sub
    where a topic = (type + source) is an indirection over agent IDs. Maps 1:1
    onto queen→worker routing by `(host, profile)`. AG2 = Apache-2.0 community
    fork; MS is folding AutoGen+Semantic Kernel into "Agent Framework" (RC early
    2026), so AutoGen-the-brand is a moving target — the *Core primitives* are the
    durable idea. (LangGraph=graph/shared-state, CrewAI=role-orchestration — neither
    is our shape.)
  - **MCP = ADOPT** for the queen's tool/context integration (orthogonal to
    transport; universal, LF-governed Dec 2025, first-party in Claude Agent SDK).
  - **Shared vector memory = BUY/borrow the patterns, not build governance.** It's
    the classical blackboard pattern (Nii 1986) modernized. Prior art: **Mem0**
    (agents share one memory instance keyed by scope), **Zep/Letta** memory blocks.
    Three primitives to adopt rather than reinvent (NirDiamant Agent-Memory notebook
    + arXiv 2505.18279 "Collaborative Memory"): (1) namespace partitioning w/
    per-partition ACLs; (2) explicit concurrent-write conflict resolution
    (last-write-wins vs optimistic version-checking — write supplies current
    version, stale writes rejected); (3) provenance/staleness handling. A June-2026
    preprint (**MemClaw**, arXiv 2606.24535, Apache-2.0) reframes multi-agent memory
    as a distributed-systems problem → a CENTRALIZED governed memory service over
    authenticated REST in a single-coordinator pattern = architecturally OUR QUEEN.
    (Caveat: single non-peer-reviewed self-marketing preprint; treat as thesis.)
  - **A2A = RESERVE for cross-fleet (queen↔queen) only** — the one place P2P interop
    is warranted; complements/alternates with the Telegram bot-to-bot federation
    edge already recorded above.
  - **Open gaps the research could NOT close:** (a) whether ANY production OSS ships
    an authenticated-WS agent mailbox in a star shape (closest = that unofficial
    a2a-rust WS dispatcher); (b) whether a centralized REST memory service beats
    just embedding a vector store IN the queen process below N workers — governance
    overhead may not pay off small; (c) NONE of the verified sources covered
    loop-prevention or delivery guarantees (at-least-once/exactly-once, TTLs,
    dead-letter) — so mailbox delivery semantics are ours to spec (ties to the
    bot-to-bot loop-prevention already flagged). Full report:
    `/tmp/claude-1000/-home-me-gh-fleetd/50a5c29e-15d2-4849-9e2e-1cfc1179d282/tasks/wb0dnj0qu.output`.

- **DECIDED 2026-07-05: shared vector memory becomes a first-class fleetd
  capability (a future phase, NOT folded into the P2 queen/worker split).** Owner
  confirmed it's a good idea. Shape (from the survey verdict): a queen-hosted,
  centrally-governed semantic memory substrate (blackboard) that workers/agents
  read+write through the queen — reuse Mem0-style scoping + namespace/ACL +
  version-checked writes rather than building governance from scratch. Deliberately
  sequenced AFTER the transport/topology lands (P2) and after talk-back (P3); slot
  as its own phase. Decide embedded-in-queen vs standalone REST service (MemClaw-style)
  when scoping — start embedded for a small fleet unless N workers justifies the split.

- **Kafka EVALUATED and REJECTED (2026-07-05) for all five candidate roles.**
  Scale/shape mismatch: fleetd is a small star-shaped mostly-synchronous command
  system (1 queen, handful of workers, `max_concurrent` 8), Kafka is for
  large decoupled high-throughput streaming meshes. Per-role: (A) transport — no,
  commands are addressed RPC-with-ack to one worker; WS already gives bidirectional
  channels + NAT traversal via Caddy + mutual-auth + presence-for-free. (B) durable
  event/audit log — no, SQLite (already present) suffices; if multi-consumer
  replayable fan-out ever appears, reach for NATS JetStream / Redis Streams (single
  binary) before Kafka. (C) shared-memory backbone — no, all prior art (Mem0,
  namespace/ACL/version-check, MemClaw) is CRUD-over-a-store, not an event log.
  (D) cross-fleet federation — no, Telegram bot-to-bot + A2A already right-sized for
  the rare queen↔queen edge. Revisit ONLY if fleetd ever becomes multi-tenant SaaS
  with hundreds of concurrent agents.

- **NORTH STAR set 2026-07-05: fleetd as an autonomous agent "company," human as
  CEO.** Owner wants the fleet to mirror a working organization. Key architectural
  insight that makes this ADDITIVE, not a teardown: **communication topology and
  org hierarchy are ORTHOGONAL.** The star topology (`no worker↔worker`, every node
  trusts only the queen) is a SECURITY decision and SURVIVES fully — org
  relationships are logical, so manager→report messages route THROUGH the queen
  (queen becomes the switchboard, as the existing memory already anticipated). Only
  "queen is a dumb router" bends slightly: it gains a routing table + agent registry
  but **stays non-LLM** — management is an agent *behavior*, not a queen feature.
  Settled model:
  - **Persistent managers = durable STATE, not long-running processes.** A manager
    is a durable identity (role, system-prompt, inbox, memory scope, org position)
    the queen persists, **rehydrated into a fresh ephemeral agent on demand**, which
    acts then terminates. So there are STILL no long-running agents — a manager
    invocation is just an ephemeral spawn seeded with durable state (actor model +
    persisted state + on-demand activation). Preserves fleetd's entire process model
    (spawn/kill/worktree, containerized queen, workers-do-the-spawning); adds only
    state management on the queen. **At most one live invocation per manager**
    (messages queue) — actor single-thread guarantee, no split-brain.
  - **Ephemeral ICs** = today's per-task agents, now tagged with a role, hired by a
    manager's `delegate(role, task)` (→ queen brokers the spawn), report back, die.
  - **Autonomy = A (autonomous by default) with EARNED tightening.** New managers
    start gated (propose→CEO approves in Telegram, reuses P3 gated-ops brake); a
    track record widens the autonomy/token/spawn budget envelope. Trust is a
    consequence of performance, not a static switch.
  - **Growth/ranks/mentorship decoded to substance (not gamification):** "learns its
    field" = manager memory scope grows; "ranks/XP" = competence metrics — build ONLY
    the ones that drive a decision (autonomy width, task routing, mentor eligibility);
    "teaches new hires" = expertise transfer via memory promotion + seeding new ICs
    with the role's distilled playbook. CEO dialogue + preferences are a first-class
    memory source. **CAVEAT (owner-resolved):** "agents get better with experience"
    is unproven — accumulated memory can bloat/mislead — UNLESS a feedback/curation
    loop closes it. The **"sleep cycle / memory defragmentation"** (rank → generalize
    → compact; cf. Generative Agents reflection, MemGPT) IS that loop; plausibly a
    queen-scheduled nightly agent (Claude Code cron). Treat "measurably better over
    tenure" as a HYPOTHESIS TO TEST, not a foundation to assume.
  - **Decomposition (each its own spec→plan→build cycle, dependency order):**
    **L0 Mailbox** (queen-routed agent-addressed messaging: addressing, inboxes,
    at-least-once delivery, loop-prevention + depth cap — the literal "email for
    agents") → **L1 Shared memory A/B/C** (sqlite-vec behind a `MemoryStore` seam;
    task scratchpad / company wiki / CEO query; incl. the consolidation/sleep cycle)
    → **L2 Persistent managers** (durable identity + on-demand rehydration) →
    **L3 Delegation** (`delegate` → broker spawn → route result back) →
    **L4 Earned autonomy + reputation** → **L5 Mentorship** (mostly L1 applied).
  - **FIRST SPEC = L0 Mailbox** (foundation everything needs; extends the existing
    `EventSink`/`CommandSource` WS seam; forces solving addressing+delivery+
    loop-prevention, the exact gap the agent-comms survey flagged as unsolved).
    Being designed now (brainstorm 2026-07-05).

- **Usage-limit handling = PARK & RESUME (recorded 2026-07-05).** The `claude` CLI
  has NO native pause/resume: on a Pro/Max plan usage-limit hit it "blocks further
  requests until the reset time" and the process terminates (confirmed via docs
  errors.md/headless.md). fleetd must build: Supervisor detects the limit event in
  the agent's stream, parses the reset time from the message ("…resets 3:45pm" /
  weekly), marks the task `parked-until <reset>` (NOT failed); queen notifies the
  CEO in Telegram. **Auto-resume via `--resume <session_id>` DOES work in headless
  `-p` mode → P4 (resume-after-restart)**; session-id lookup is scoped to the
  working dir + its worktrees, so the resume re-spawn MUST reuse the SAME worktree
  (fits fleetd's worktree model). **VERIFY EMPIRICALLY before building detection**
  (undocumented, mirror the stdin-gotcha verify style): exact stream-json event
  shape + exit code on a limit hit. Distinct from context-window-full, which the
  CLI handles itself via compaction (no fleetd action). Profile plan limits are
  INDEPENDENT (owner-confirmed) but route-around-exhaustion is MOSTLY N/A — see the
  profile↔repo binding constraint below.

- **Vasya integration = QUEEN-side surface, NOT a worker endpoint (north-star
  adjacency, recorded 2026-07-05).** Vasya is the owner's other project: a Jarvis-
  like voice assistant on a Windows laptop that runs AI to manage the host. Q: can
  local host agents reach the fleetd worker on their host? A: by design NO — the
  worker dials OUT to the queen and its only localhost server (the L0 MCP shim) is
  bound to one spawned agent's `ref`; exposing the worker breaks "trusts exactly one
  peer" + adds a local RCE authz surface. Integration goes to the QUEEN (or a local
  queen), three patterns, star-clean in all: **(A) Vasya as a voice CEO surface** —
  peer to Telegram, authenticated under the same owner-lock; the queen can route
  `ceo` messages to voice so the fleet TALKS BACK (= the P3 talk-back / `human-loop`
  surface, by voice). **(B) Vasya as a host-agent via MCP** — expose host tools
  (speak/files/apps) as an MCP server that fleet agents consume (fits the "adopt MCP"
  decision; stdio/HTTP, Windows-friendly); optionally a first-class L0-mailbox
  participant (connects to the queen, never a worker). **(C) local queen on the
  laptop** — the real answer if offline/low-latency is needed: Vasya→local queen
  (localhost, offline-OK) which FEDERATES with the homeserver queen (the recorded
  multi-fleet federation path). CAVEAT: Vasya has powerful host capability → fleet
  agents driving it = blast-radius expansion → gate behind P3/L4 gated-ops +
  owner-lock. Own future thread (its own brainstorm); does NOT affect the L0 spec.

## Gotchas

- **`--permission-prompt-tool` was REMOVED in `claude` 2.1.201.** The Phase-3
  gated-ops brake must use a **blocking PreToolUse hook** (returns allow/deny),
  not that flag. Verified absent from `claude --help` 2026-07-04.


- **`claude -p … --input-format stream-json` BLOCKS on stdin until EOF.** In
  headless one-shot mode the CLI reads its task as stream-json user messages from
  stdin and hangs forever if stdin is an open pipe that's never written/closed.
  Verified live 2026-07-04: with `--input-format stream-json` the process emitted
  0 events and never exited; without it, `claude -p "<task>" --output-format
  stream-json --verbose` streams a result and exits rc=0. **Phase 1 deliberately
  omits `--input-format` and uses `stdin=DEVNULL`** (see `agent.py._argv` /
  `start`). Phase 2 soft-steer must reintroduce `--input-format stream-json`
  *and* actually write a stream-json user message to stdin + keep the pipe
  managed — don't naively re-add the flag. (Soft-steer is now **Phase 3**, after
  the queen/worker topology.)

## Constraints / conventions

- **Auth is non-negotiable:** every Telegram update rejected unless
  `from_user.id == config.owner_id`. Enforced structurally via a
  `dp.update.outer_middleware` (owner check before routing) PLUS per-handler
  `F.func(owner_only)` as defense-in-depth. Any new handler must not become an
  ungated path.
- **All outbound Telegram text is MarkdownV2** (global `ParseMode.MARKDOWN_V2`).
  Escape every dynamic value with `formatting.escape_md`, or send plain replies
  with `parse_mode=None`. Unescaped repo names / result text (which routinely
  contain `-` `.` `(` `)`) cause Telegram 400 "can't parse entities".
- Phase 1 is `native` mode only, single process. Phase 2 = queen/workers (see
  `2026-07-04-fleetd-phase2-queen-workers-design.md`). `ask_human` MCP,
  soft-steer, gated-ops approval → Phase 3; sandbox, resume-after-restart,
  worktree cleanup → Phase 4. **Shared vector memory (queen-hosted blackboard) →
  its own later phase, after P2/P3** (decided 2026-07-05; see Decisions).
- **Profile↔repo binding (owner-confirmed 2026-07-05): the work profile
  (`~/.claude-work`) operates ONLY on work-related repos; personal (`~/.claude`) on
  personal repos.** A task's repo dictates its eligible profile — profiles are NOT
  interchangeable labor pools. Plan usage limits are INDEPENDENT per profile
  (separate OAuth accounts), but you generally CANNOT dodge a rate-limited profile
  by rerouting its work, because the eligible profile is fixed by the repo:
  work-repo + work-profile-exhausted ⇒ PARK (no personal fallback). The L4
  "route around the exhausted division" idea only applies if a task is ever
  profile-agnostic or a class ever has >1 account — not the case today.
