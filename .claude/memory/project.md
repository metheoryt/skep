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
  worktree cleanup → Phase 4.
