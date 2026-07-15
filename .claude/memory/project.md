# skep — project memory

<!-- Repo-local, git-tracked, auto-loaded at session start. Durable project facts
only (decisions, gotchas, constraints). One bullet per fact. No secrets. -->

## Decisions

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

- **Phase 2 Plan 1 EXECUTED 2026-07-05** (branch `skep-phase2`, formerly
  `gortex-align`; local-only repo, no remote). All 9 TDD tasks landed: `Config`
  split into `WorkerConfig`/`QueenConfig`; `transport.py` seam
  (`EventSink`/`CommandHandler`/`QueenInbox` + `InMemoryEventSink`); formatting
  descriptors now emit PLAIN text (escaping moved to the queen); agent
  `CLAUDE_CONFIG_DIR` injection; `Supervisor` emits domain events + `max_concurrent`
  cap; `queen/` package (`bookkeeping.py` ref-mapping SQLite, `telegram_sink.py`
  QueenSink, `router.py` QueenRouter); interim single-process `app.py` wiring queen
  +worker over the in-memory transport (`build_worker_and_router`, `parse_spawn`,
  owner-gated `/spawn`/`/ls`/`/kill`/`/panic`). 69 tests pass. **Plan 2 WRITTEN
  2026-07-05** (`plans/2026-07-05-skep-phase2-plan2-websocket-transport.md`, 12
  TDD tasks: config net/auth fields → `wire.py` codec → `auth.py` HMAC handshake →
  queen WS server + `RemoteWorker` + `on_spawn_rejected` → worker WS client +
  `WsEventSink` + `SwitchableEventSink` → heartbeat/presence/detached-`/ls` →
  reconnect/backoff + idempotent topic re-attach → `discovery.py` mDNS →
  `skep-queen` entrypoint → `skepd` entrypoint → queen group auto-onboarding →
  two-worker WS e2e + auth-reject). Deps added: `aiohttp`, `zeroconf`. **Deviation
  from design §15:** `telegram_gw.py`/`formatting.py` stay at `src/skep/` (NOT moved
  into `queen/`) to dodge gotcha (b) below. Containerized-queen/Caddy VPS wiring is
  explicitly OUT of scope (lives in `~/gh/vps`).
  **Plan 2 EXECUTED 2026-07-05** (subagent-driven, all 12 TDD tasks; merged to `main`
  at merge commit `78f872e`, feature branch `feat/skep-phase2-plan2` deleted; 117
  tests pass `-m "not mdns"` + mDNS round-trip 4/4 local, `uvx pyright src` clean).
  New modules: `wire.py`, `auth.py`, `ws_transport.py`, `discovery.py`, `queen/app.py`,
  `worker/app.py`, `queen/onboarding.py`. AUTH IS NOW 4 FRAMES (challenge/auth/auth_ok/
  auth_error) — `handshake_server` sends `auth_error` before rejecting so a peer parked
  on recv() under gather doesn't deadlock. Whole-branch (opus) review caught+fixed 3
  cross-task bugs per-task reviews missed: reconnect-clobber race (→ `QueenRouter.
  detach_if_current` compare-and-clear), empty `shared_secret` failed OPEN (→ both
  `serve()` fail-closed with SystemExit on empty/whitespace secret), and an unguarded
  re-attach replay loop (→ per-item try/except). DEFERRED follow-ups (not blocking,
  logged): wedged-worker liveness eviction (§6.4 K-overdue sweeper — only transport
  ping/pong exists today); `wire.LS_REPLY`/`LS_REQUEST` are unused (no live `/ls` query
  path — `/ls` reads bookkeeping); minor test-hygiene nits.
  **L0 MCP-shim spike RESOLVED 2026-07-05** — doc
  `docs/superpowers/specs/2026-07-05-l0-mcp-shim-spike.md`. Decisions (verified vs
  `claude` 2.1.201): shim = **in-worker-process streamable-HTTP MCP server** on
  `127.0.0.1` (NOT stdio — stdio would be an agent-child with no handle to the seam),
  **one server per worker** multiplexed by a **per-agent bearer token** (token→tid
  map; enforces §11 spoof-proof `from`), injected via `--mcp-config '<inline JSON>'`
  at spawn WITHOUT `--strict-mcp-config` (agent keeps profile MCP e.g. gortex). Bind
  to **worker-local `tid`** (sync at spawn), NOT queen `ref` (async/fire-and-forget);
  queen resolves `from`→ref via existing bookkeeping. THE REAL NEW MACHINERY: the seam
  is fire-and-forget both ways but mailbox tools are **request/reply** → add a `req_id`
  + `dict[req_id,Future]` correlation layer on the WS (new frames `mailbox_send`/
  `mailbox_ack`/`inbox_read`/`inbox_reply`); persist-before-ack; **L1 memory reuses
  this exact layer**. Link-down → shim returns retryable error (never hangs). fake_claude
  CAN'T call MCP → real tool round-trip is integration/manual; unit-test shim handlers +
  seam req/reply directly. Defaults: 20/min, depth 10, dedupe 60s, body 16KB, pure-pull
  inbox.
  **L0 MAILBOX BUILT + MERGED to main 2026-07-05 (merge `92f0c3a`, branch
  `feat/skep-l0-mailbox`, 26 commits).** Plan `docs/superpowers/plans/2026-07-05-l0-mailbox.md`
  (13 TDD tasks, subagent-driven w/ per-task + whole-branch review). Shipped:
  `queen/mailbox.py` (Mailbox store + MailboxService policy pipeline),
  `queen/addressing.py` (ceo/mgr:<name>/<ref>, fail-closed to active IC only),
  `worker/mcp_shim.py` (per-agent FastMCP streamable-HTTP, owns uvicorn lifecycle),
  `WsMailboxClient` req/reply layer in `ws_transport.py`, config knobs, CEO
  outbound (MarkdownV2-escaped) + inbound reply. Reconciled from the spike:
  **one FastMCP app PER AGENT on an ephemeral port with `tid` closed over**
  (not one-server-per-worker+token — token unused, `mcp_token=None`); identity
  still spoof-proof (closure + server-side `agent_sender`). 202 tests pass (+1
  opt-in real-claude integration `SKEP_RUN_INTEGRATION=1`), pyright-clean.
  Reviews caught+fixed real bugs: read_inbox archive race, depth-cap bypass via
  unresolvable in_reply_to, shim socket-leak on stop + failed-start, supervisor
  spawn failure-path leak, MarkdownV2 TelegramBadRequest (bot default parse mode),
  reply-id injection misrouting, and the whole-branch BLOCKER (worker assembly
  never wired the mailbox — feature was inert).
  **L0.1 HARDENING DONE 2026-07-05 (branch `feat/skep-l0.1-hardening`, 3
  commits `8021c0c`/`5499247`/`01e712b`; 214 tests, src pyright-clean; adversarial
  review ran + fixed):** (1) DONE at-least-once CEO delivery — acceptance
  decoupled from Telegram push; `Mailbox.pending()` non-destructive peek;
  `MailboxService.redeliver_ceo()` drains pending CEO mail in order, marks read
  only after a successful push, under an `asyncio.Lock` (no double-push); periodic
  `_ceo_retry_loop` (SKEP_MAILBOX_CEO_RETRY_INTERVAL, 30s) tied to the aiohttp app
  lifecycle. Review caught a CRITICAL regression: a body >4096 chars (within the
  16384-byte cap) is a permanent Telegram 400 → drain-all-stop-at-first wedged the
  whole CEO queue forever → fixed with `PermanentDeliveryError` (deliver_ceo maps
  TelegramBadRequest→permanent; redeliver dead-letters+alerts+skips permanent,
  only retries transient). `_safe_alert` stops a failed alert crashing the
  pipeline. (2) DONE per-agent shim bearer token — `secrets.token_urlsafe(32)`
  per spawn; `_require_bearer` ASGI middleware (constant-time, 401, non-http
  passthrough); token never logged/persisted. CAVEAT: token rides the agent's
  argv (`--mcp-config`), so a SAME-UID sibling can read it from /proc/cmdline —
  relocating off-argv is same-UID-defeatable too (env/file also same-UID-readable)
  and inline `--mcp-config` `${VAR}` expansion is unconfirmed, so NOT done; it's
  defense-in-depth vs passive port-scan, fully effective only under UID isolation.
  **NEW L0.2 FOLLOW-UP: per-agent UID/sandbox isolation** (the real fix for
  co-located spoofing; deliver the shim token off-argv at the same time).
  **L0.1 CLOSE-OUT DONE 2026-07-05 (branch `feat/skep-l0.1-closeout`, 3 TDD
  commits; 217 tests pass `-m "not mdns"`, `uvx pyright src` 0 errors, ruff
  clean):** the three still-open L0.1 items are fixed. (5) `MailboxShim.stop()`
  now `except (Exception, SystemExit)` — deliberately NOT bare `BaseException`,
  so uvicorn's bind-collision SystemExit is swallowed but `CancelledError` still
  propagates. (3) recipient-gone TOCTOU: `handle_send` re-checks IC recipient
  liveness (`resolve_address`) right after `insert` and dead-letters if the
  agent went terminal — DEFENSE-IN-DEPTH, not a live bug (verified: the WS
  `_dispatch_mailbox_send` awaits `handle_send` inline on the queen event loop
  and `handle_send` runs resolve→insert with ZERO awaits between, so `on_done`→
  `handle_recipient_gone` can't interleave today; the guard closes the window if
  an await is ever added there). (4) single-process path had an inert mailbox
  (switch built, target never set → `MailboxUnavailable`): extracted the queen's
  MailboxService assembly into **`src/skep/queen/assembly.py`**
  (`build_mailbox_service` + `make_ceo_callbacks`/`_ceo_retry_loop`/
  `_install_ceo_retry`/`_mailbox_db_path`), shared by BOTH `build_queen` and the
  single-process `app.main`; `build_worker_and_router` takes an optional
  `mailbox_service` and points the switch at an `InMemoryMailboxClient`; `main`
  threads it through QueenSink/build_dispatcher and runs the CEO-retry sweeper as
  a background task (no aiohttp app on the polling path). `assembly.py` NEVER
  imports `skep.app` → no import cycle (`queen.app` still imports `build_dispatcher`
  from `skep.app`); the CEO helpers are re-exported from `skep.queen.app` for
  existing import sites. `main()` glue itself is untested (blocks on
  `start_polling`) — verified at the `build_worker_and_router` assembly seam, not
  end-to-end. Minor recorded asymmetry: `InMemoryMailboxClient.send` lets
  `agent_sender`'s `ValueError` (unknown tid) propagate, whereas the WS path
  returns a clean rejected ack — unreachable in practice (bk row exists by spawn
  time) and pre-existing to L0.1 #4.
  **Next step: L1 memory (reuses the req/reply layer), or L0.2 UID isolation.**
  Two execution gotchas from Plan 2 (kept for reference): (a) the plan
  predated this repo's pyright governance, so plan-faithful rewrites regressed
  `src` type-cleanliness — keep `src` pyright-clean (0 errors; `uvx pyright src`),
  mirror the `_task()` assert-helper + `Callable[...]` factory annotations idiom;
  (b) the `Config`→`QueenConfig` split forced migrating `telegram_gw.py` (a coupling
  the gortex-annotation commit had introduced) — watch for similar type-annotation
  couplings when Plan 2 moves modules to the queen.

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
  **DOCUMENTED ASSUMPTION (load-bearing):** gortex has **no per-profile scope** (one
  daemon per user per machine). Profile isolation holds ONLY because personal
  (`~/.claude`) and work (`~/.claude-work`) live on separate WSL distros with separate
  daemons + tracked-repo sets. Co-locating both profiles on one host silently leaks a
  work repo's operational notes to a personal agent — revisit BEFORE that happens.
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
- **Profile↔repo binding (owner-confirmed 2026-07-05): the work profile
  (`~/.claude-work`) operates ONLY on work-related repos; personal (`~/.claude`) on
  personal repos.** A task's repo dictates its eligible profile — profiles are NOT
  interchangeable labor pools. Plan usage limits are INDEPENDENT per profile
  (separate OAuth accounts), but you generally CANNOT dodge a rate-limited profile
  by rerouting its work, because the eligible profile is fixed by the repo:
  work-repo + work-profile-exhausted ⇒ PARK (no personal fallback). The L4
  "route around the exhausted division" idea only applies if a task is ever
  profile-agnostic or a class ever has >1 account — not the case today.
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
