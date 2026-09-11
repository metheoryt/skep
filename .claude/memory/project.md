# skep — project memory

<!-- Repo-local, git-tracked, auto-loaded at session start. Durable project facts
only (decisions, gotchas, constraints). One bullet per fact. No secrets. -->

## Decisions

- **Sessions A3 — usage-limit park & auto-resume.** `ARCHITECTURE.md` §6
  (Sessions table) and §7 own the status and the term-by-term map
  (`parked`/`parked_until`, `detect_usage_limit` and its heuristic residual, the
  park sweep, `origin="sweep"`, the deferred P2 multi-account pool and P3
  per-subagent model). Five things live only here, because each is a mechanism
  that is easy to re-break:
  (1) **Dedup lives in `Supervisor.resume`, not `cmd_resume`.** `cmd_resume`'s
  `status != 'running'` is a cheap FILTER — it never writes status, and `running`
  is only set when the worker's `task_started` round-trips into
  `rebind_invocation`, so two callers can race. The real claim is
  `Supervisor._live_sessions: set[int]`, taken with no intervening `await` and
  released in both `resume`'s failure branch and `run_events`' `finally`; a
  duplicate raises `ValueError`. The spec asserted the opposite and was corrected
  (§5/§6).
  (2) **`build_worker_and_router` must call `router.mark_online(...)`.** Without
  it the in-process worker read as detached forever and the sweep — which skips
  offline workers — auto-resumed nothing.
  (3) **The two shapes reject differently, and both are load-bearing.**
  `RemoteWorker.resume` is fire-and-forget (returns 0, never raises), so
  rejections come back later as a `spawn_rejected` frame; single-process raises
  `CapacityError`/`ValueError` synchronously into the sweep's own `except`.
  (4) **`origin` rides the resume frame and is echoed into `spawn_rejected`**
  (alongside `action`, so the verb matches). Without it a full worker produced one
  owner Telegram message every sweep tick — 30 s apart, forever. A human's
  `/resume` still notifies, because it answers optimistically and the failure
  arrives async.
  (5) **`Supervisor.resume` seeds the new row with
  `resume_token=prev.resume_token`** — `latest_invocation` is
  `ORDER BY id DESC LIMIT 1`, so a tokenless newest row made every later resume
  fail permanently.
  **Residual:** sweep-origin suppression is TOTAL — a permanently-failing parked
  entry retries every tick forever at INFO with no give-up counter, and the
  exception type is not a usable discriminator ("already has a live invocation"
  is benign).

- **L0.2 Increment 1 DONE (2026-07-16, branch `feat/skep-l0.2-increment1`,
  re-planned against current main after the abandoned
  `feat/skep-l0.2-per-agent-isolation` went stale under 54 commits of L1.1 +
  Sessions).** Two surface-reduction mechanisms for spawned agents: (1) **env
  hygiene** — `agent._agent_env` is now a **default-drop allowlist**
  (`_CORE_ENV_KEYS`=PATH,HOME,USER,LOGNAME,TERM,LANG,TZ,SHELL + all `LC_*` +
  `_OPTIONAL_ENV_KEYS`=SSL_CERT_*/NIX_SSL_CERT_FILE/LOCALE_ARCHIVE/*_PROXY/
  NO_PROXY + lowercase), replacing `dict(os.environ)`; drops the whole `SKEP_*`
  namespace, `ANTHROPIC_*`, `CLAUDE_CODE_*`/`CLAUDECODE`; `CLAUDE_CONFIG_DIR` set
  from the arg only. Opt-in widen via `SKEP_AGENT_ENV_PASSTHROUGH` →
  `WorkerConfig.agent_env_passthrough` (bypasses the drop-list — never list a
  secret). (2) **token off argv** — the whole N-server MCP map (mailbox http+
  bearer + memory stdio) is written to a `0600` `<worktree>/.skep/mcp.json` via
  the new `worker/mcp_config.py::write_mcp_config` (fchmod-before-write; git-
  info/exclude'd) and passed as `--mcp-config <path>`; the per-agent bearer no
  longer rides `/proc/<pid>/cmdline`. Injectable `Supervisor(mcp_config_writer=)`
  seam. **Empirically confirmed on real claude (~/.claude, this host):** spike
  (a) scrubbed-env auth + Bash-tool completion PASS (keep-set SUFFICIENT, no
  widening; dropping ANTHROPIC_* is safe — agents auth from the profile), and
  token-off-argv + memory-stdio-under-scrubbed-env e2e PASS. **Honest residual:**
  closes only the different-UID / other-local-user vector (cmdline is world-
  readable). A **same-UID sibling** can still read the worker's
  `/proc/<worker-pid>/environ` OR the 0600 file (same owner) — full same-UID
  containment awaits **Increment 2's PID/mount namespaces**. **FOLLOW-UP for
  attach/primary (Increment 2 / A2):** `.skep/mcp.json` has a FIXED filename;
  fine today (only `MODE_NEW` roots exist, tid-unique worktrees), but concurrent
  agents against a shared persistent repo would clobber each other's token file
  → silent 401. Must become tid-keyed (`mcp-<tid>.json`) before attach/primary
  roots go live (documented at the write site in `supervisor.py`).

- **Agent memory is tracked repo files** (`<repo>/.agent-memory/*.md`, one fact
  per file), read into the spawn addendum for free and written through a stdio
  `remember` MCP tool. Supersedes L1's gortex-daemon store, deleted 2026-07-09.
  skep grants its agents `Bash,Edit,Write` + exact MCP tool names on argv —
  before this, agents on this host had **no permissions at all** and the
  MailboxShim was unreachable in production.

- **PROJECT RENAMED `fleetd` → `skep` (2026-07-05), branch `skep-phase2`
  (formerly `fleetd-phase2`/`gortex-align`).** Hive metaphor: a *skep* is a
  traditional woven beehive — the vessel that houses the colony (queen + workers,
  which are the literal roles in the code). Chosen for being free everywhere
  (PyPI + GitHub) after `apiary` (great fit but collides with apiary.io/Oracle)
  and fleet-metaphor variants were considered. Package `src/skep/`, console
  script `skep`, env prefix `SKEP_`. Plan-2 binary names: worker daemon `skepd`,
  queen `skep-queen` (was `fleetd`/`fleetqueen`). Deploy domain `skep.cyphy.kz`
  (was `fleet.cyphy.kz`). Upstream: `git@github.com:metheoryt/skep.git`. The
  gortex auto-generated skills table (`CLAUDE.md`/`AGENTS.md`/`.claude/skills/
  generated/`) still says "fleetd" — machine-managed, regenerates on reindex.
  NOTE: the English noun "fleet" (a fleet of agents) intentionally survives in
  prose; only the `fleetd`/`fleetqueen`/`FLEETD_` tokens were renamed.

- **Build-vs-buy (evaluated 2026-07-04): decided to BUILD skep despite heavy
  overlap with two official Anthropic features.** `claude remote-control --spawn
  worktree --capacity N` (worktree-isolated agent fleet driven from claude.ai /
  Claude mobile app, outbound-only HTTPS, `--sandbox`) covers much of skep's
  core; the official **Channels** Telegram plugin (`/plugin install
  telegram@claude-plugins-official`, pairing + allowlist auth) covers the
  Telegram+owner-lock piece but only pushes into ONE running session (no
  spawn/fleet/worktree/kill, no tool-by-tool streaming). Owner is on **Pro/Max
  claude.ai OAuth**, so Remote Control IS available to them — they chose to build
  anyway. **Differentiator that justifies skep:** Telegram-native *fleet*
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
  the Telegram front lives). See `2026-07-04-skep-phase2-queen-workers-design.md`.
  Key settled decisions:
  - **Queen + workers are SEPARATE scripts/processes** (`skep-queen`, `skepd`),
    explicit roles — NO leader election / auto-failover / consensus. The bot
    token-as-lock `409` check is only a startup guard against an accidental
    double-queen.
  - **Bot token lives ONLY on the queen.** Workers hold only the WS shared secret
    (smaller blast radius). Queen owns all Telegram I/O + formatting
    (`telegram_gw.py`/`formatting.py` move to the queen); workers emit domain
    events over a WebSocket via an `EventSink`/`CommandSource` seam.
  - **Queen is containerized on the homeserver behind Caddy** (`skep.cyphy.kz`
    → `reverse_proxy 10.0.0.2:8765`, WS upgrade is transparent); **workers stay
    native** (agents need host access). Queen never spawns agents, so it
    containerizes cleanly.
  - **Discovery: 3 tiers, same shared-secret auth** — mDNS (`_skep-queen._tcp`)
    on the LAN, `--queen-url wss://skep.cyphy.kz` over internet, WG direct
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
  in-memory transport seam, 9 TDD tasks) + Plan 2 (WebSocket +
  entrypoints + mutual auth + mDNS + heartbeat/presence + queen auto-onboarding +
  deploy — NOT yet written).

- **Phase 2 (queen/worker split + WS transport) and L0/L0.1 (mailbox +
  hardening) are SHIPPED and merged.** For *what is built and what is not*, read
  `ARCHITECTURE.md` §7 — it owns the Phase 1–4 / L0–L5 status tables and is
  overwritten in place. What follows is only the set of decisions and incidents
  that section does not carry. Structurally: `transport.py` is the seam
  (`EventSink`/`CommandHandler`/`QueenInbox` + `InMemoryEventSink`), with
  `wire.py`, `auth.py`, `ws_transport.py`, `discovery.py`, `queen/app.py`,
  `worker/app.py`, `queen/onboarding.py` on top; formatting descriptors emit
  PLAIN text and escaping happens on the queen. **`telegram_gw.py` /
  `formatting.py` deliberately stayed at `src/skep/` instead of moving into
  `queen/`** — the `Config`→`QueenConfig` split had coupled them through
  type annotations, and moving them dragged that coupling along. Watch for
  similar annotation couplings before relocating a module.

- **Four WS-transport rules, each of which cost a real bug:**
  - **Auth is FOUR frames** (`challenge`/`auth`/`auth_ok`/`auth_error`).
    `handshake_server` sends `auth_error` *before* rejecting, so a peer parked on
    `recv()` under `gather` does not deadlock.
  - **An empty or whitespace `shared_secret` must fail CLOSED** — both `serve()`
    paths raise `SystemExit`. It used to fail open.
  - **Reconnect-clobber race → `QueenRouter.detach_if_current`**, a
    compare-and-clear: a late disconnect from a superseded connection must not
    clear the live one.
  - **Idempotent topic re-attach needs per-item `try`/`except`** — one bad item
    otherwise turns the replay into an unguarded loop.
  All three of the first bugs were caught by a **whole-branch (opus) review that
  per-task reviews missed**; run one before merging a multi-task branch.

- **The L0 MCP shim is one FastMCP streamable-HTTP app PER AGENT**, on an
  ephemeral `127.0.0.1` port, with the worker-local `tid` **closed over** (not
  one server per worker keyed by token). Identity is therefore spoof-proof by
  construction (closure + server-side `agent_sender`), which is why the spike's
  token→tid map was dropped. Bind to the worker-local `tid`, synchronously at
  spawn — NOT the queen's `ref`, which is assigned async/fire-and-forget; the
  queen resolves `from`→`ref` through bookkeeping. Injected via `--mcp-config`
  **without** `--strict-mcp-config`, so the agent keeps its profile MCP servers
  (e.g. gortex). The seam is fire-and-forget both ways but mailbox tools are
  request/reply, so there is a `req_id` + `dict[req_id, Future]` correlation
  layer on the WS (`mailbox_send`/`mailbox_ack`/`inbox_read`/`inbox_reply`),
  persist-before-ack, link-down returns a retryable error and never hangs.
  **L1 memory reuses that exact layer.** Mailbox policy defaults: 20/min, depth
  10, dedupe 60 s, body 16 KB, pure-pull inbox. `fake_claude` cannot call MCP, so
  a real tool round-trip is integration/manual — unit-test the shim handlers and
  the seam's req/reply directly.

- **Four mailbox-delivery rules that are load-bearing:**
  - **A body over 4096 chars is a PERMANENT Telegram 400, and it is inside the
    16384-byte cap** — so a drain-all-stop-at-first loop wedged the whole CEO
    queue forever. `deliver_ceo` maps `TelegramBadRequest` →
    `PermanentDeliveryError`; redelivery dead-letters + alerts + skips permanent
    failures and retries only transient ones. `_safe_alert` keeps a failed alert
    from crashing the pipeline. CEO delivery is at-least-once and decoupled from
    the Telegram push: `Mailbox.pending()` is a non-destructive peek,
    `redeliver_ceo()` marks read only after a successful push, under an
    `asyncio.Lock`.
  - **`MailboxShim.stop()` catches `(Exception, SystemExit)`, deliberately NOT
    bare `BaseException`** — uvicorn's bind-collision `SystemExit` is swallowed
    while `CancelledError` still propagates.
  - **The recipient-gone re-check in `handle_send` is defence-in-depth, not a
    live bug.** Verified: the WS `_dispatch_mailbox_send` awaits `handle_send`
    inline on the queen loop and `handle_send` runs resolve→insert with ZERO
    awaits between, so `on_done`→`handle_recipient_gone` cannot interleave today.
    The guard closes the window the moment an `await` is added there.
  - **`src/skep/queen/assembly.py` must NEVER import `skep.app`** — it is the
    shared MailboxService assembly (`build_mailbox_service`, the CEO retry
    helpers, `_mailbox_db_path`) used by BOTH `build_queen` and the
    single-process `app.main`, and `queen.app` still imports `build_dispatcher`
    from `skep.app`. Importing back creates a cycle. Before this existed the
    single-process path had an inert mailbox (switch built, target never set →
    `MailboxUnavailable`) — a whole feature silently unwired, which is also what
    the whole-branch review caught in L0 itself.

- **Keep `src` pyright-clean (0 errors, `uvx pyright src`).** Plan-faithful
  rewrites regressed it once because the plan predated this repo's pyright
  governance; mirror the `_task()` assert-helper and the `Callable[...]` factory
  annotation idiom rather than reintroducing `Any`.

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
    `/tmp/claude-1000/-home-me-gh-skep/50a5c29e-15d2-4849-9e2e-1cfc1179d282/tasks/wb0dnj0qu.output`.

- **DECIDED 2026-07-05: shared vector memory becomes a first-class skep
  capability (a future phase, NOT folded into the P2 queen/worker split).** Owner
  confirmed it's a good idea. Shape (from the survey verdict): a queen-hosted,
  centrally-governed semantic memory substrate (blackboard) that workers/agents
  read+write through the queen — reuse Mem0-style scoping + namespace/ACL +
  version-checked writes rather than building governance from scratch. Deliberately
  sequenced AFTER the transport/topology lands (P2) and after talk-back (P3); slot
  as its own phase. Decide embedded-in-queen vs standalone REST service (MemClaw-style)
  when scoping — start embedded for a small fleet unless N workers justifies the split.

- **Kafka EVALUATED and REJECTED (2026-07-05) for all five candidate roles.**
  Scale/shape mismatch: skep is a small star-shaped mostly-synchronous command
  system (1 queen, handful of workers, `max_concurrent` 8), Kafka is for
  large decoupled high-throughput streaming meshes. Per-role: (A) transport — no,
  commands are addressed RPC-with-ack to one worker; WS already gives bidirectional
  channels + NAT traversal via Caddy + mutual-auth + presence-for-free. (B) durable
  event/audit log — no, SQLite (already present) suffices; if multi-consumer
  replayable fan-out ever appears, reach for NATS JetStream / Redis Streams (single
  binary) before Kafka. (C) shared-memory backbone — no, all prior art (Mem0,
  namespace/ACL/version-check, MemClaw) is CRUD-over-a-store, not an event log.
  (D) cross-fleet federation — no, Telegram bot-to-bot + A2A already right-sized for
  the rare queen↔queen edge. Revisit ONLY if skep ever becomes multi-tenant SaaS
  with hundreds of concurrent agents.

- **NORTH STAR set 2026-07-05: skep as an autonomous agent "company," human as
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
    persisted state + on-demand activation). Preserves skep's entire process model
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
    **DESIGNED — spec committed `docs/superpowers/specs/2026-07-05-l0-mailbox-design.md`
    (2026-07-05).** But L0 is BLOCKED on unbuilt foundations: it rides the Phase-2
    Plan-1 transport seam (`EventSink`/`CommandHandler`) — **DONE 2026-07-05 (the
    in-memory seam ships; see the Plan-1-executed bullet)** — plus Plan 2 (real WS)
    for anything past in-memory tests, plus an UNRESOLVED spike (the worker-local
    MCP shim + agent↔`ref` binding, spec §15). **Build order: Phase 2 Plan 1 (the
    seam) — DONE; Plan 2 (real WS transport) — DONE + merged 2026-07-05; then resolve
    the shim spike as L0's first task, then build L0.** The mailbox brainstorm ran ahead
    of the build sequence — that's fine, the design is banked. Shim spike RESOLVED
    2026-07-05 (see the Plan-2-executed bullet + `2026-07-05-l0-mcp-shim-spike.md`);
    the next EXECUTABLE step is now building L0 Mailbox (TDD).

- **Usage-limit handling = PARK & RESUME (recorded 2026-07-05).** The `claude` CLI
  has NO native pause/resume: on a Pro/Max plan usage-limit hit it "blocks further
  requests until the reset time" and the process terminates (confirmed via docs
  errors.md/headless.md). skep must build: Supervisor detects the limit event in
  the agent's stream, parses the reset time from the message ("…resets 3:45pm" /
  weekly), marks the task `parked-until <reset>` (NOT failed); queen notifies the
  CEO in Telegram. **Auto-resume via `--resume <session_id>` DOES work in headless
  `-p` mode → P4 (resume-after-restart)**; session-id lookup is scoped to the
  working dir + its worktrees, so the resume re-spawn MUST reuse the SAME worktree
  (fits skep's worktree model). **VERIFY EMPIRICALLY before building detection**
  (undocumented, mirror the stdin-gotcha verify style): exact stream-json event
  shape + exit code on a limit hit. Distinct from context-window-full, which the
  CLI handles itself via compaction (no skep action). Profile plan limits are
  INDEPENDENT (owner-confirmed) but route-around-exhaustion is MOSTLY N/A — see the
  profile↔repo binding constraint below.

- **Vasya integration = QUEEN-side surface, NOT a worker endpoint (north-star
  adjacency, recorded 2026-07-05).** Vasya is the owner's other project: a Jarvis-
  like voice assistant on a Windows laptop that runs AI to manage the host. Q: can
  local host agents reach the skep worker on their host? A: by design NO — the
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

- **L1 DESIGNED 2026-07-09 — "agent memory is gortex memory; skep stores nothing."**
  Spec `docs/superpowers/specs/2026-07-09-l1-memory-substrate-design.md` (revision 2,
  commit `9771365`). Revision 1 (commit `4fab2d7`, superseded in-file) specified a
  queen-hosted SQLite/FTS5 store + 4 WS frames + per-scope ACL matrix + a `repo_key`
  protocol change across 6 modules — over-built; owner pushed back ("does it look too
  complicated?"). `gortex memory store|recall|surface` already provides per-repo
  (workspace) and per-machine (global) scopes, supersedes, importance, tags,
  provenance, ranked recall — and already runs on every worker box. skep's whole
  contribution is **one `--append-system-prompt` addendum at spawn** naming the repo
  path + when to write, plus a **startup preflight** that OMITS the addendum when the
  daemon is down / `gortex` off PATH / repo untracked (never hand an agent a command
  that fails). Per-task scratch = a file in the worktree (no mechanism). CEO↔agent =
  the L0 mailbox (not a memory scope). `SKEP_MEMORY_ENABLED` (default true) forces off.
  **Memory is an ENHANCEMENT, not critical path** — an agent without it still does its
  task, so the gortex dependency is soft and must never fail a spawn.
  VERIFIED 2026-07-09 (gortex v0.56.0), incl. two claims that proved FALSE:
  (a) Anthropic has NO first-party embeddings endpoint (docs → Voyage AI) — this
  killed the sqlite-vec plan; (b) **gortex memory does NOT work from an agent's
  worktree** (agents run at `worktrees_root/<repo>-<tid>`; daemon tracks only the
  parent repo → `store` errors "daemon does not track …"). Fix: `--index <repo_path>`
  — verified store+recall from a live worktree, lands in the parent's workspace.
  (c) The **MCP** path (profile gortex survives because spawn omits
  `--strict-mcp-config`) is **UNVERIFIED** — same cwd-coverage gate, unknown whether
  `store_memory` takes a repo override. The spec deliberately depends on the **CLI**
  (workers are native → `gortex` on PATH → agent reaches it via Bash). Verify
  separately before ever relying on MCP.
  **DOCUMENTED ASSUMPTION, NOW BROKEN (re-checked 2026-09-11):** gortex has **no
  per-profile scope** (one daemon per user per machine). Profile isolation used to
  hold because personal (`~/.claude`) and work (`~/.claude-work`) lived on separate
  WSL distros with separate daemons + tracked-repo sets. **That premise is gone:**
  the fleet ships exactly one committed profile (`settings.json` → `~/.claude`; the
  `~/.claude-pure` work profile was folded back in `machines` commit `d48c09a`), and
  on g513ie only `~/.claude` exists — work and personal repos are already co-located
  under one daemon. So the leak this paragraph said to revisit BEFORE it happened
  has happened: a personal agent can recall a work repo's operational notes. Decide
  whether skep must scope memory itself, or whether the repo-path `--index` argument
  is enough of a boundary.
  **DEFERRED, not discarded:** the 2026-07-05 queen-hosted central store remains the
  end state (L2's persistent managers need queen-persisted durable identity); its
  complexity buys cross-machine sharing, which this fleet doesn't need yet. Trigger to
  build: agents on different hosts, or co-located profiles, needing shared memory —
  NOT "the store feels small". Sleep cycle + vectors likewise deferred with triggers.
  **NEXT STEP: write the implementation plan** (superpowers:writing-plans) from the
  spec, then TDD it. Test surface is small: `_argv` includes/omits the addendum per
  preflight; spawn succeeds in every unavailable case; addendum's recommended
  invocation === the string preflight smoke-checks (guards drift).

- **SESSIONS design (multi-provider evolution, sub-project A) DESIGNED + A1 BUILT
  & MERGED 2026-07-11.** Spec `docs/superpowers/specs/2026-07-10-sessions-design.md`
  makes **Session** a first-class primitive: three concepts — **Session** (pinned
  execution context: host/profile/runner/workspace/worktrees; owns a Telegram topic;
  fleet-global `ref`; parkable/resumable) / **Invocation** (one runner run; holds
  `resume_token` + `model`) / **Manager** (durable identity above sessions; continuity =
  memory+inbox, NOT a transcript; rehydrated fresh). Split along a §3 ownership boundary
  into **A1 (worker-side, DONE)** and **A2 (queen-side, NEXT)**.
  - **A1 = worker Invocations.** Plan
    `docs/superpowers/plans/2026-07-10-sessions-a1-worker-invocations.md`; 12 commits,
    merged to `main` (head `e6d1deb`, pushed to origin), subagent-driven w/ per-task +
    Opus whole-branch review. Shipped: DB migration (`session_id`→`resume_token`, add
    `model`/`session_local_id`, `PRAGMA user_version`, back-fill `session_local_id=id`)
    + invocation-grouping queries; `AgentProcess` renders `--add-dir`/`--model`/`--resume`;
    `workspace.py` `Root`/`Workspace` value types + `requires_lease` predicate; multi-root
    memory (project-targeted write, unioned read; all L1.1 security invariants preserved);
    `Supervisor.spawn_workspace` (multi-root+model+session_local_id) with `spawn` a thin
    backward-compat wrapper; `Supervisor.resume` (new invocation, same worktree, v1-minimal
    = resume_token+model+BASE_TOOLS only, no memory/mailbox). Suite 338 passed / 2 skipped.
    **A1 delivers CAPABILITY, not visible behavior** — resume/multi-root/`--model` are
    reachable only through new methods no caller yet exercises; the queen is untouched
    except one ride-along wire field.
  - **REF-KEYING DECISION (load-bearing, non-obvious — the code embodies it but does not
    self-document why):** the spec says the worker's invocation is "keyed by session ref",
    but the queen mints `ref` only AFTER `task_started` and `RemoteWorker.spawn` returns
    literal `0` — so at first-spawn the worker cannot key by a ref that doesn't exist yet.
    Resolution: the WORKER owns a local `session_local_id`; a first invocation's
    `session_local_id == its own task id`; a resume reuses the originating session's id;
    **A2 maps `ref → (host, profile, session_local_id)`** (extends the existing
    `Bookkeeping.by_worker_task`). This honors the ownership split without inverting the
    working fire-and-forget spawn protocol. The single A1→A2 wire interface is one OPTIONAL
    field: `task_started` carries `session_local_id` (queen ignores it until A2).
  - **A2 = queen-side (NEXT sub-project; own spec→plan→build cycle).** Scope: session
    registry (`ref ↔ session_local_id` map), **lease enforcement for `primary:rw`** (A1
    ships the `requires_lease` predicate but never acquires), visibility/inheritance, and
    **topic-follows-session**. Start from the spec's B/C/E seam interfaces + the A1 plan.
  - **A2 HANDOFF GAP (flagged by the A1 whole-branch review; NOT an A1 defect):** the
    register-replay `_active_payload` in `ws_transport.py` does NOT carry `session_local_id`,
    so an A2 queen reconnect would lose session identity on replayed active tasks. Fold into
    the A2 plan (thread `session_local_id` through the replay payload when A2 consumes it).
  - Later Sessions sub-projects (not started): B (runner seam — pydantic-ai alongside
    headless Claude Code), C (fleet capability catalog / name→path resolution), D (session
    spawning), E (Telegram role + probes). SDD task-by-task record + deferred Minors live in
    `.superpowers/sdd/progress.md` (git-ignored local scratch — not synced).
  - **On merge (still TODO):** README's "Agent memory" section and ARCHITECTURE.md §7 +
    the L0–L5 ladder get rewritten by the Sessions model.

- **SESSIONS A2 (queen-side), Tasks 1–9 of 9 DONE 2026-07-22, branch
  `metheoryt/ubuntu26-skep-develop` (not yet merged to `main`).** Ships the
  registry + the `--watch` slice, NOT the full A2 scope the design spec lays out.
  **Shipped:** (1) `Bookkeeping` entries are session-scoped — `session_local_id`
  column, `PRAGMA user_version` migration backfilling `session_local_id=local_id`,
  `by_session()` + `rebind_invocation()`. (2) `QueenSink.on_task_started` reuses a
  known session's `ref` and Telegram topic instead of opening a second one — the
  topic follows the session. **This branch has no live caller until `/resume`
  exists; it is tested directly, not through any command path.** (3) The A1→A2
  HANDOFF GAP flagged above (line ~520) is now CLOSED: `session_local_id` rides
  the worker's register/heartbeat `active_tasks` payload and the queen's replay
  loop (`QueenWsServer._replay_active`), so a reconnecting A2 queen no longer
  loses session identity on replayed active tasks. (4) `worker/roots.py`:
  `resolve_roots(repos_root, specs) -> Workspace`, the security gate mapping root
  NAMES (never paths) to paths under the worker's own `repos_root`; refuses,
  never downgrades — bad names, unknown mode/access, `primary:rw` (needs a lease,
  not built), `attach` (no shared-worktree registry yet), a non-`new` head root,
  an empty/malformed spec list. (5) `Supervisor.spawn(roots=...)` — absent
  `roots` is byte-identical to pre-A2 behavior. (6) the spawn wire frame carries
  `roots` as NAMES ONLY (never paths — `--add-dir` is an arbitrary-read
  primitive, so a path on the wire would hand a rogue queen a read primitive
  over the worker's whole disk); a refusal returns through the existing
  `spawn_rejected` frame. (7) `access="ro"` binds skep's own write paths: the
  memory shim gets rw roots only, reads still union every root (watching the
  checkout IS the point), and `readonly_declaration` skips any path an rw root
  also resolves to — so skep never tells the agent a directory it writes memory
  into is read-only. Known consequence: the canonical same-name `--watch` pair
  has BOTH roots resolve to one path, so this guard is inert there; it only
  binds when the watched repo has a different name (proven in
  `tests/test_integration.py::test_watch_spawn_reaches_the_agent_argv`, which
  deliberately uses two differently-named roots to pin the declaration reaching
  argv, not the inert same-name case). (8) `/spawn <host> [--profile p] <repo>
  [--watch] <task>` — `--watch` is opt-in on purpose: it composes the two-root
  workspace, and a watched checkout may hold uncommitted secrets the operator
  never intended an agent to read. **Deferred, with reasons, not forgotten:** the
  `primary:rw` lease table (nothing in this slice opens such a root — A2 refuses
  the mode outright); parking/`/resume` and the resume state machine;
  `visible`/`spawn_visibility` enforcement (governs children, arriving with
  sub-project D); sub-project C's fleet catalog and `workspaces.yaml` (the
  worker resolves names from its own `repos_root` instead, sidestepping C for
  now); topic close/reopen ↔ park/resume binding (sub-project E owns it).
- **Buzz (Block, open-sourced 2026-07-21) surveyed 2026-07-25 — skep CONTINUES;
  the execution plane is the asset, the Telegram layer is provisional.** Full memo:
  `docs/2026-07-25-buzz-comparison-memo.md`. Buzz is a Nostr-relay collaboration
  substrate (signed event log, per-participant keypairs, channels, NIP-34 git,
  YAML workflows, ACP harness). Its seam sits ABOVE skep's: it has NO concept of
  host, credential profile, or per-task worktree, and no usage-limit park/resume —
  i.e. it duplicates skep's queen/Telegram half (`Bookkeeping` would evaporate; it
  exists only because the Bot API can't read topics back) and none of the worker
  half where A1–A3 and L0.2 went. Three consequences: (1) do **Sessions B (runner
  seam) next**, before more Telegram-side work; (2) **ACP is the seam's SHAPE, not
  a replacement for the `claude` path** — Claude Code has no native ACP surface,
  Buzz reaches it via the third-party `@agentclientprotocol/claude-agent-acp`
  wrapping the *Agent SDK* with `ANTHROPIC_API_KEY`, which would cost subscription
  auth, `CLAUDE_CONFIG_DIR` profile isolation, and the `stream-json` events that
  `parse_event`/`detect_usage_limit` need; keep native `claude -p` as
  implementation #1, ACP as #2 for Codex/Goose; (3) stop investing in
  `Bookkeeping`-shaped Bot-API workarounds — new control-surface work should assume
  the surface may change. Borrow list beyond ACP, ranked: per-agent keypair + signed
  action log (the missing substrate under L4 reputation), agents as durable room
  members rather than per-task topics (that IS L2), workflow triggers + approval
  gates as data (skep's unbuilt Phase 3 brakes). Adopting Buzz wholesale as the
  front end is last: Postgres+Redis+S3/MinIO+relay vs today's `uv run skep`, on a
  four-day-old project with one non-replicated relay. Discriminator if this is ever
  revisited: multi-participant audited workspace ⇒ invert skep (keep the worker,
  drop Telegram, join a relay); private single-operator fleet driver ⇒ Buzz replaces
  nothing.

## Gotchas

- **Both former entries here were `claude` CLI behaviour, not skep facts, and
  moved up to `~/.claude/memory/global.md` under
  `## Harness behavior (empirical)`:** `--permission-prompt-tool` was REMOVED in
  `claude` 2.1.201, and `claude -p … --input-format stream-json` BLOCKS on stdin
  until EOF. skep's two consequences stay here: a Phase-3 gated-ops brake must be
  a **blocking `PreToolUse` hook** (allow/deny), never that flag; and **Phase 1
  deliberately omits `--input-format` and uses `stdin=DEVNULL`**
  (`agent.py._argv` / `start`), so the Phase-3 soft-steer must reintroduce
  `--input-format stream-json` *and* actually write a stream-json user message to
  stdin *and* keep the pipe managed — don't naively re-add the flag.

## Constraints / conventions

- **`ARCHITECTURE.md` (repo root) is the single steady-state concept map — added
  2026-07-10 because specs/plans record *history*, not the present.** It is
  hand-written and **overwritten in place**: never add a dated copy, never move it
  under `docs/superpowers/specs/` (that directory is chronological by design and
  reproduces the problem). It carries a branch+commit stamp at the top; when it
  disagrees with code, the code wins and the file gets fixed. It is the only place
  that says the two numbering axes are orthogonal (Phase 1-4 = control-plane build
  phasing; L0-L5 = north-star capability layers, "L" = Layer) and that the beehive
  and corporate metaphors coexist. Keep the sharp-edges section bounded — it is an
  orientation doc, not a cleanup backlog.

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
  `2026-07-04-skep-phase2-queen-workers-design.md`). `ask_human` MCP,
  soft-steer, gated-ops approval → Phase 3; sandbox, resume-after-restart,
  worktree cleanup → Phase 4. **Shared vector memory (queen-hosted blackboard) →
  its own later phase, after P2/P3** (decided 2026-07-05; see Decisions).
- **Profile↔repo binding (owner-confirmed 2026-07-05): the work profile operates
  ONLY on work-related repos; personal (`~/.claude`) on personal repos.** A task's
  repo dictates its eligible profile — profiles are NOT interchangeable labor
  pools. Plan usage limits are INDEPENDENT per profile (separate OAuth accounts),
  but you generally CANNOT dodge a rate-limited profile by rerouting its work,
  because the eligible profile is fixed by the repo: work-repo +
  work-profile-exhausted ⇒ PARK (no personal fallback). The L4 "route around the
  exhausted division" idea only applies if a task is ever profile-agnostic or a
  class ever has >1 account — not the case today. **The dir name in this rule is
  stale and there is currently no second profile to bind to** (checked
  2026-09-11): the committed `settings.<postfix>.json` set IS the profile
  registry and only `settings.json` → `~/.claude` is committed, so nothing
  provisions a `~/.claude-work`; the `pure` / `~/.claude-pure` profile was folded
  back into `settings.json` in `machines` commit `d48c09a`, and g513ie carries
  only `~/.claude`. Work-account separation is Orca's account switcher now, not a
  config dir — so read this as a rule about *accounts*, and re-derive the
  mechanism before building routing on it.
- **Type view = ty + ruff (adopted 2026-07-09, per the reworked `gortex-align`
  skill).** `[tool.ty]` + `[tool.ruff]` in `pyproject.toml`; standalone
  `pyrightconfig.json` removed. `uvx ty check src` is the resolution gate (clean);
  `uvx ruff check` (with `ANN`) is the annotation-presence gate. ty is pre-1.0
  (v0.0.x) — its config keys/rule names can churn; re-verify against
  `uvx ty check --help` if a key errors. **FOLLOW-UP:** `uvx ruff check src` still
  has 21 findings — 18 missing return-type annotations (ANN204 on `__init__`/dunder,
  ANN202 on private fns) across ~10 files + 3 E501 long lines. Deferred, not
  gortex-blocking (ty is already clean); annotate to feed the native provider's
  annotation-presence half when convenient.
