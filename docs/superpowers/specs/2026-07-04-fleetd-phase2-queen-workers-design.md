# fleetd Phase 2 — Queen + Isolated Workers (Design)

**Date:** 2026-07-04
**Status:** Approved design, pre-implementation
**Supersedes:** the Phase 2 scope in `2026-07-04-agent-fleet-control-plane-design.md` §7
(originally "talk-back + brakes"). See §1.

## 1. Context & re-scoping

### 1.1 Build-vs-buy revisit (the project memory flagged this "before Phase 2/3")

Re-evaluated 2026-07-04 against the current CLI (`claude` 2.1.201) and docs.
**Outcome: continue building.** Findings:

- **Remote Control** (`--remote-control`, `claude agents`, `--worktree`,
  `--spawn worktree --capacity N`, `--sandbox`) genuinely covers talk-back,
  steering, worktrees, fleet, and sandbox — **but only from Anthropic's own
  surfaces (claude.ai / mobile app), never Telegram.**
- The official **Channels** Telegram plugin is single-session: no spawn/list/kill,
  no per-tool streaming.
- **The gap is real and unserved:** Telegram-native control of a *fleet* of
  headless agents, with live per-task streaming, fully self-hosted. Phase 1
  already delivers the core of that gap.

Also discovered: **`--permission-prompt-tool` no longer exists in 2.1.201.** The
Phase-3 gated-ops brake will use a **blocking PreToolUse hook** instead (noted
here so the Phase-3 plan doesn't rebuild on a dead flag).

### 1.2 New phasing

The original Phase 2 ("talk-back + brakes") is **deferred to Phase 3**, because a
topology decision now comes first: the fleet spans **multiple hosts** (g16,
latitude5520, homeserver), and talk-back's reply/approval routing depends on
where the Telegram front-end lives. So:

| Phase | Scope |
|---|---|
| **2 (this doc)** | **Queen + isolated workers.** Split Phase-1's single process into a Telegram-owning **queen** and one or more **workers**; multi-host, discovery (LAN + public link + WG), per-worker profile isolation, capacity cap. |
| 3 | Talk-back + brakes: `human-loop` MCP (`ask_human`/`notify_human`), soft-steer, gated-ops approval via a blocking PreToolUse hook. Rides the Phase-2 transport seam. |
| 4 | Sandbox (container-per-agent). |
| Later | Cross-agent message bus; auto-failover/leader-election (explicitly *not* in Phase 2 — see §12). |

## 2. Topology

```
Internet ──► VPS (Caddy + WireGuard, cyphy.kz) ──tunnel──► Homeserver 10.0.0.2
                        │                                        │
                 fleet.cyphy.kz                            fleetqueen (container)
                 reverse_proxy 10.0.0.2:8765  ───────────► :8765  WS server + bot
                                                                 ▲
                        ┌────────────────────────────────────────┤ WebSocket
                        │ (mDNS on LAN · wss://fleet.cyphy.kz over internet · WG direct)
        ┌───────────────┴───────────────┬──────────────────────┐
   fleetd host=g16              fleetd host=g16          fleetd host=latitude5520
          profile=personal            profile=work            profile=default
   CLAUDE_CONFIG_DIR=~/.claude  =~/.claude-work          (native, spawns agents)
```

One **queen** owns Telegram; **workers** run agents and connect out to the queen.
Queen and workers are **separate scripts/processes** — the role is explicit, never
elected. A single machine may run the queen *and* one or more workers.

## 3. Roles

### 3.1 Queen (`fleetqueen` — new entrypoint)

The only Telegram-aware component. Responsibilities:
- Owns the bot token and the **sole `getUpdates`** long-poll.
- **Owner-ID lock** (rejects every update whose sender ≠ owner) — same posture as
  Phase 1, now enforced here.
- All Telegram I/O + formatting: `telegram_gw.py` and `formatting.py` **move to
  the queen**. Creates/deletes per-task topics, posts/edits the live activity
  message and milestones.
- **WS server** (`aiohttp`) accepting worker connections, authenticated by a
  shared secret.
- **mDNS advertise** (`_fleetd-queen._tcp.local.`) so LAN workers self-discover it.
- **Command router:** `/spawn <host> …`, `/ls` (fan-out + aggregate), `/kill
  <host:id>`, `/panic` (broadcast).
- **Bookkeeping SQLite** — one row per task with columns `(host, profile,
  local_task_id, topic_id, activity_msg_id)` and its own autoincrement `ref` used
  as the global task handle in the UI (§8). This is the *only* extra state, and it
  exists because the **Telegram Bot API cannot read topics back** (no
  `getForumTopics`, no history fetch); after a restart the queen can't re-derive
  topic mappings from Telegram, so it persists them. It is a legitimate structured
  store, not an identity-decode map — `host`/`profile` are always carried as
  fields, never packed into a string.
- Runs **containerized on the homeserver** behind Caddy (it spawns no agents, so
  it containerizes cleanly — see §10).

### 3.2 Worker (`fleetd` — refactored from Phase 1)

Runs agents; never talks to Telegram. Responsibilities:
- Keeps `Supervisor`, `agent.py`, `stream.py`, and a **local** registry (its own
  tasks, worktrees, sessions, pids — survives restart for future resume).
- **Discovers the queen**: mDNS browse on the LAN, or an explicit
  `--queen-url wss://fleet.cyphy.kz` (over-internet), or a WG address.
- **WS client** (outbound, NAT-friendly, shared-secret auth) to the queen.
- Emits domain events through an `EventSink` (§5) instead of calling a Gateway.
- **No bot token** — only the shared secret. Fewer copies of the most sensitive
  credential.
- Runs **native** on each machine (agents need real host access).
- Enforces a **capacity cap** (§6.3) and **profile isolation** (§4).

## 4. Multi-worker isolation (first-class)

A worker scopes every agent it spawns to a Claude **profile** by injecting
`CLAUDE_CONFIG_DIR` (plus a clean env) into the `claude` subprocess. This makes
"personal claude" and "work claude" two independent workers on one host.

**Per-worker config** (one file per worker, e.g. `personal.toml` / `work.toml`;
secrets via env override, never in the file):

**A worker's identity is the structured pair `(host, profile)` — two separate
fields, never a concatenated/parsed string and never a decode map.** Both flow
through the protocol, routing, and bookkeeping as distinct fields (§7, §8).

| Field | Purpose |
|---|---|
| `host` | Machine identity. Default = hostname (e.g. `g16`). |
| `profile` | Profile label on that host (e.g. `work`, `personal`). Default = `default`. |
| `claude_config_dir` | `CLAUDE_CONFIG_DIR` injected into agents → selects the Claude profile (skills, creds, gortex MCP, hooks, settings). |
| `repos_root` | Where this profile's repos live. |
| `worktrees_root` | Per-worker worktree area (no cross-profile collisions). |
| `db_path` | Per-worker registry SQLite. |
| `max_concurrent` | Capacity cap (§6.3). |
| `queen_url` / mDNS | How to reach the queen. |
| `shared_secret` (env) | WS auth. |

`profile` is a human-facing label; `claude_config_dir` is the mechanism. They're
configured independently, so the label and the path need not correlate.

**Isolation guarantee:** a `personal` worker's agent can never read
`~/.claude-work` credentials, and vice-versa. Work secrets (e.g. the work Sentry
key in each work repo's project-scope `.claude/settings.local.json`, which Claude
reads natively) reach only `work`-profile agents.

**Deployment:** two templated units — `fleetd@personal`, `fleetd@work` — each with
its own config/env, coexisting on the host. The queen sees them as two workers
that share a `host` but differ in `profile`. MVP rule: **one worker = one
profile/config**; mixed-profile-within-one-worker is a later nicety, not now.

## 5. The transport seam

Two interfaces decouple core logic from the wire, and give tests a fake transport:

- **`EventSink`** (worker → queen): `task_started(task_id, repo, title)`,
  `activity(task_id, line)`, `milestone(task_id, text)`, `done(task_id, status,
  summary)`, `ls_reply(tasks)`.
- **`CommandSource`** (queen → worker): `spawn(repo, task)`, `kill(task_id)`,
  `panic()`, `ls_request()`.

`Supervisor.run_events` (Phase 1) is refactored to emit these domain events
instead of calling `gateway.post/edit`, and **drops topic-id bookkeeping** (the
queen owns topic + activity-message ids now).

There is **one production transport — WebSocket** — used the same way whether the
worker is co-located (`ws://127.0.0.1:8765`) or remote. Tests use an in-memory
fake implementing the same interfaces.

## 6. Discovery, connection, capacity

### 6.1 Three ways a worker reaches the queen (same auth, different reach)
1. **mDNS/DNS-SD** — zero-config on the home LAN. Queen advertises
   `_fleetd-queen._tcp.local.` (SRV host+port, TXT `host`/`ver`); workers browse,
   resolve, and dial. Same-subnet only (multicast doesn't route); Wi-Fi client
   isolation / VLANs block it.
2. **Public link** — `--queen-url wss://fleet.cyphy.kz`. Works from anywhere over
   the internet via Caddy (§10). This is "add a worker over-internet": hand it the
   link + shared secret. First-class: queen config carries `public_url`.
3. **WireGuard** — a peer on the WG mesh dials `wss://10.0.0.2:8765` directly, no
   public exposure.

### 6.2 Reconnection & re-attachment
Worker retries with backoff; on mDNS "queen removed"/re-added it re-resolves and
reconnects. Queen sends an mDNS goodbye on shutdown. On reconnect the worker
re-`register`s as the **same `(host, profile)`** and **re-reports its active
tasks** (their local ids); the queen re-attaches each to its existing topic via
the bookkeeping `ref`. This is the same re-attachment path as a queen restart
(§13).

### 6.4 Liveness / presence — who's online
Two layers:
- **Transport (built-in):** aiohttp WS ping/pong via `heartbeat=N` (~20s) on both
  `ws_connect` (worker) and `WebSocketResponse` (queen). Detects dead peers
  including *half-open* drops a clean TCP close misses (NAT idle-timeout, laptop
  sleep, cable pull). The queen's **online set = registered workers with a live
  WS**.
- **Application (added):** worker → queen `heartbeat {active_tasks,
  capacity_remaining}` every ~20s. Beyond the socket ping it (a) catches a *wedged*
  worker whose socket is alive but loop stalled, and (b) keeps the queen's fleet
  view fresh (capacity/task counts) for `/ls` and the Phase-3 status board without
  polling. Queen tracks `last_seen`; overdue-by-K + no pong → close and mark
  offline.

**Correctness invariant:** *worker offline ≠ its agents dead.* A dropped link
leaves the `claude` processes running on the host; only reporting stops. On
disconnect the queen marks the **worker** offline (its tasks shown as
"detached/last-known" in `/ls`) but **never** marks those tasks failed/killed —
they resume reporting on reconnect (§6.2).

### 6.3 Capacity
Per-worker `max_concurrent` (default 8). A single worker already runs **N agents
in parallel** (one `AgentProcess` asyncio task each — Phase 1 behavior). Over the
cap → the spawn is rejected with a clear message to the queen (queueing deferred).
This bounds CPU/RAM/API-rate on a laptop. Parallelism does **not** require multiple
workers; multiple workers are for *isolation* (§4), not throughput.

## 7. Wire protocol (WebSocket, JSON messages)

- **worker → queen:** `register {host, profile, version, capabilities,
  active_tasks:[...]}` (re-lists active tasks so a reconnect re-attaches — §6.2),
  `heartbeat {active_tasks, capacity_remaining}` (~20s — §6.4),
  `task_started {task_id, repo, title}`, `activity {task_id, line}`,
  `milestone {task_id, text}`, `done {task_id, status, summary}`,
  `ls_reply {tasks:[...]}`, `spawn_rejected {reason}`.
- **queen → worker:** `spawn {repo, task}`, `kill {task_id}`, `panic {}`,
  `ls_request {}`.
- Phase 3 extends this with `ask_human`, `notify_human`, `permission`, `reply`,
  `answer`, `approval`.

The WS connection is bound to its worker's `(host, profile)` at `register`, and
`task_id` in every subsequent message is that worker's local id — the queen never
parses identity out of a string; it reads `host`/`profile` as fields and pairs
them with the connection.

## 8. Identity & routing

- **`host` and `profile` are separate everywhere** — separate config fields (§4),
  separate protocol fields (§7), separate columns in bookkeeping (§3.1), separate
  columns in `/ls`. Nothing concatenates them into a parseable id.
- **Task reference for by-id commands is the queen's global `ref`** (the
  bookkeeping autoincrement, §3.1) — an opaque integer, e.g. `/kill 42`. No
  host/profile string to parse; the queen looks the `ref` up to find
  `(host, profile, local_task_id)`.
- Topics stay per-task; the queen maps `topic → ref`, so topic-scoped actions
  (kill button; Phase-3 replies) need no id at all.
- Commands:
  - `/spawn <host> [--profile <p>] <repo> <task>` — `host` positional, `profile`
    an optional flag defaulting to `default` (or the host's sole worker if
    unambiguous). Repo + task follow.
  - `/ls` — renders `ref`, `host`, `profile`, `repo`, `status` as columns, plus a
    presence marker per worker (online / detached, with `last_seen` — §6.4).
    Detached workers' tasks show as last-known, not dead.
  - `/kill <ref>`, `/panic` (broadcast to all workers).
- `/ls` with no connected workers, or a `/spawn` to an unknown host/profile, gets a
  clear error naming the missing worker.

## 9. Security

The queen is now a **network service that can spawn agents on every worker** — a
`spawn` command *is* arbitrary code execution on the target worker. So the
worker↔queen channel is a fleet-wide RCE surface and must be both **encrypted**
and **mutually authenticated**. First-class concerns:

- **Owner-ID Telegram lock** on the queen (unchanged from Phase 1) — only the
  owner can drive the queen.
- **Encryption, per hop:**
  - Internet → **`wss://`** via Caddy's TLS (real cert). 
  - WireGuard mesh → the **WG tunnel** encrypts (inner `ws://` is fine).
  - Plain-LAN `ws://` is the one unencrypted case — prefer `wss://`/WG; on a
    trusted LAN it is acceptable *only* because the auth below still protects
    integrity. Eavesdropping on that hop remains possible, so treat it as the
    weakest link.
- **Mutual authentication via challenge-response (mandatory), not a bearer
  token.** Both sides prove knowledge of the shared secret over exchanged nonces
  (HMAC), so:
  - a **rogue/spoofed queen** (e.g. a forged `_fleetd-queen` mDNS advert on the
    LAN, or a hijack of `fleet.cyphy.kz`) that lacks the secret **cannot** get a
    worker to accept `spawn`/`kill` — this closes the scariest vector (fleet-wide
    RCE via a fake queen);
  - a **rogue worker** cannot impersonate a host to intercept its commands or
    inject fake events.
  Nonces prevent replay. The worker must treat an mDNS-discovered address as
  untrusted until the handshake completes.
- **Secret handling:** a long random value in the queen's env and each worker's OS
  secret store — never committed. Per-worker secrets (vs one fleet secret) are a
  hardening option if intra-fleet impersonation ever matters.
- **Bot token lives only on the queen** (workers don't have it).
- **Blast radius, stated honestly:** the queen can spawn on any worker *by design*
  — it is the crown jewel. Its compromise = fleet-wide RCE. Mitigations: owner-ID
  lock, secret protection, and the **Phase-3 gated-ops brakes** that bound what a
  spawned agent may do.
- **Optional hardening** (noted, not required for MVP): Caddy **mTLS client certs**
  or `basic_auth` in front of `fleet.cyphy.kz`, so only credentialed workers can
  reach the queen at all.
- **Profile-credential isolation** per §4.

## 10. Deployment

- **Queen** → homeserver container, matching the existing `homeserver/<svc>/
  compose.yml` + `.env.dist` pattern (like Tugtainer/`AGENT_SECRET`). Listens
  `:8765`. Caddy vhost: `fleet.cyphy.kz { reverse_proxy 10.0.0.2:8765 }` — Caddy
  transparently upgrades WebSocket, so `wss://` works with TLS for free. Tugtainer
  auto-updates it.
- **Workers** → native processes (systemd user units on NixOS; Scheduled Task /
  service wrapper on Windows), one per profile.
- **Co-location** — the homeserver may run the queen container *and* a native
  worker (worker → `ws://127.0.0.1:8765`). A single dev machine runs a worker
  (and, if it's the front, the queen).
- The actual VPS wiring (Caddyfile vhost + `homeserver/fleetqueen/compose.yml` +
  `.env.dist`) lives in `~/gh/vps` and is a **separate deployment change**, not
  part of this repo's implementation. This spec fixes the contract: queen listens
  `:8765`, reads `FLEET_SHARED_SECRET`, advertises `public_url`.

### 10.1 Queen group onboarding (Plan 2)

The queen can self-onboard to its control group instead of Phase-1's hardcoded
`group_chat_id` + manual README setup. Verified against the Bot API (2026-07-04):

- **Auto-doable:** discover groups it's added to / promoted in via `my_chat_member`
  updates (learns each `chat_id`); register its commands per-chat via
  `setMyCommands` + `BotCommandScopeChat`; inspect readiness via `getChat.is_forum`
  and `getChatMember` (own admin rights) and **post a setup prompt** if something's
  missing.
- **NOT auto-doable (human admin action):** *enabling Topics/forum mode* — no Bot
  API method exists (`is_forum` is read-only; `toggleForum` is MTProto-only); and
  *bootstrapping its own admin / `can_manage_topics`* — a bot can't self-promote
  from non-admin.
- **Security gate:** anyone can add the bot to any group. Act only on groups where
  the **owner is a member/admin** (`getChatMember(chat_id, owner_id)`) or an
  explicit allowlist — never onboard arbitrary groups. (The owner-ID command lock
  still applies regardless.)
- **Scope:** makes `group_chat_id` optional and self-validating; **one fleet → one
  control group** — multi-group streaming is YAGNI. Queen polish, lands in Plan 2
  with the queen entrypoint. Sources in §16.

## 11. Refactor from Phase 1

| Phase-1 location | Phase-2 change |
|---|---|
| `telegram_gw.py`, `formatting.py` | Move to the queen. |
| `Supervisor.run_events` (calls `gw.post/edit`) | Emit `EventSink` domain events; drop `topic_id`/`activity_msg_id` bookkeeping (queen owns it). |
| `app.py` (bot + dispatcher + supervisor in one process) | Split: **queen app** (bot + dispatcher + router + WS server + mDNS) and **worker app** (event loop + WS client + mDNS browse). |
| `config.py` | Split into queen config (bot token, owner, group, listen addr, secret, public_url) and worker config (§4 table). File-based per-worker + env override. |
| `db.py` Registry | Stays on the worker (local task state). Queen gets a small separate bookkeeping store. |
| `agent.py` | Inject `CLAUDE_CONFIG_DIR` + clean env per agent (§4). Otherwise unchanged; `--input-format`/stdin still deferred to Phase 3. |

## 12. Topology boundaries, non-goals & the federation path

- **No worker↔worker communication — the fleet is a star.** Workers connect
  *only* to the queen; there are no worker-to-worker links. Any cross-worker
  interaction (should it ever be needed) routes *through* the queen; a true
  cross-agent bus is deferred (see below). This keeps the trust model simple:
  a worker trusts exactly one peer (its queen).
- **Auto-failover / leader election / consensus mesh** — the queen is explicit and
  single. If it dies, restart it (or start a queen elsewhere). The token-as-lock
  `409` check is only a **startup guard** against an accidental double-queen, not
  an election protocol.
- **Replicated / gossiped shared state** — none. Worker→queen event stream +
  queen aggregation + the small bookkeeping SQLite is the whole story.
- **Deferred:** **talk-back + brakes** (Phase 3), **sandbox** (Phase 4),
  **cross-agent bus** and **multi-fleet federation** (Later — but see §12.1, the
  path is now a native Telegram primitive rather than a hack).

### 12.1 Multi-fleet federation & bot provisioning (Later — now native)

Recent Telegram Bot API updates (2026) turn two previously-hacky ideas into
first-class primitives. **Neither is Phase 2**, but the deferred design now points
at real APIs instead of workarounds (this supersedes an earlier note that assumed
bot-to-bot was impossible and that only an MTProto user-account could mint bots):

- **Cross-fleet federation** (e.g. an isolated/unreachable local-queen fleet
  interacting with the main fleet "like email"): **Bot-to-Bot Communication —
  Bot API 10.0 (2026-05-08).** Two queen bots that *both* enable "Bot-to-Bot
  Communication Mode" in @BotFather can message each other by `@username` (private,
  groups, business mode) — **autonomously, no human relay, no MTProto userbot.**
  Telegram *requires the developer to implement loop-prevention* (deduplicate,
  rate-limit ~1 reply/few-sec per bot, cap interaction depth/timeouts) — that
  burden lands on whoever builds this layer.
- **Per-fleet bot provisioning** (auto-mint a bot for each new fleet's queen — the
  "our own BotFather" idea): **Managed Bots — Bot API 9.6 (2026-04-03).** A manager
  bot with Bot Management Mode enabled receives `ManagedBotUpdated` updates and
  fetches a managed bot's token via `getManagedBotToken` (rotate via
  `replaceManagedBotToken`). In *this* design only queens own bots, so this
  provisions per-**fleet**, not per-worker.

**Discipline — these do NOT change intra-fleet transport.** The worker↔queen
channel stays **WebSocket** (§5), not bot-to-bot: routing agents through Telegram
would force a bot token onto every worker (undoing §9's token-only-on-queen),
subject live streaming to Telegram rate limits + loop-prevention, and make Telegram
a hard dependency for local control. Bot-to-bot is the *cross-fleet* edge.

**One noted exception:** a *remote, low-traffic* worker with no direct network path
to the queen MAY use a **bot-to-bot uplink** (NAT-friendly, `@username`, no public
port) instead of exposing the queen over Caddy/WG — accepting Telegram's
rate-limit/latency for that one link. High-throughput workers keep WebSocket. This
is the only place a worker↔queen link would ever ride Telegram; it stays Later
scope. **Managed Bots does not replace the intra-fleet control plane** — it only
streamlines per-fleet bot provisioning and cross-network reach; the daemon still
runs on each host (Telegram features don't execute code on your machines).

**Caveat:** Bot API 9.6/10.0/10.1 are weeks-to-months old; `aiogram` may not yet
expose these methods — verify library support before building this layer (it is
deferred, so no blocker for Phase 2).

## 13. Open questions / spikes (resolve during planning/implementation)

- **WebSocket through Caddy** — confirm `reverse_proxy` upgrades WS transparently
  for `fleet.cyphy.kz` (expected yes; low risk).
- **`CLAUDE_CONFIG_DIR` isolation** — confirm it fully selects the profile
  (skills/creds/settings/gortex/hooks) for a spawned `claude -p` agent (expected
  yes; the mechanism the user already relies on for `~/.claude` vs `~/.claude-work`).
- **mDNS on the actual networks** — verify multicast works on the home LAN and
  isn't blocked by Wi-Fi client isolation; the `--queen-url` fallback covers the
  rest.
- **Queen restart topic reattachment** — on restart, the queen reloads the
  bookkeeping SQLite; workers re-`register` and re-report active tasks; confirm the
  live activity message reattaches (or posts a fresh one) cleanly.

## 14. Testing strategy

- `EventSink`/`CommandSource` fakes → unit-test `Supervisor` event emission and
  queen routing without a network.
- Queen router units: `/spawn` host/profile routing, `/ls` aggregation across ≥2
  fake workers, `/kill <ref>`, `/panic` broadcast, unknown host/profile errors.
- Worker WS client: register, backoff/reconnect, capacity rejection.
- **Mutual-auth handshake:** a queen/worker with the wrong secret is rejected by
  both sides; a replayed handshake (stale nonce) is rejected.
- mDNS: advertise+browse round-trip on loopback (or mocked `zeroconf`).
- Profile isolation: assert the spawned-agent env carries the worker's
  `CLAUDE_CONFIG_DIR` and nothing from the other profile.
- End-to-end: single process (queen+worker in one test harness over an in-memory
  transport) with the existing fake-claude stub; plus a two-worker aggregation test.

## 15. New / changed files (indicative)

```
src/fleetd/
  transport.py        # EventSink / CommandSource interfaces + in-memory fake
  ws_transport.py     # WebSocket server (queen) + client (worker)
  discovery.py        # mDNS advertise (queen) + browse (worker); --queen-url
  queen/
    __init__.py
    app.py            # bot + dispatcher + router + WS server + mDNS advertise
    router.py         # /spawn /ls /kill /panic; routes on (host, profile)
    bookkeeping.py    # ref -> (host, profile, local_task_id, topic_id, msg_id) SQLite
    (telegram_gw.py, formatting.py moved here)
  worker/
    __init__.py
    app.py            # event loop + WS client + mDNS browse + capacity
    (supervisor.py refactored to emit EventSink; agent.py env injection)
  config.py           # QueenConfig + WorkerConfig (file + env)
pyproject.toml        # + zeroconf ; console scripts: fleetqueen, fleetd
```

## 16. Sources

Telegram Bot API capabilities referenced above (federation & provisioning, §12.1):

- Telegram blog — *AI bot revolution: 11 new features* —
  <https://telegram.org/blog/ai-bot-revolution-11-new-features>
- Bot features — *Bot-to-Bot Communication*, *Managed Bots*, *Guest Bots* —
  <https://core.telegram.org/bots/features>
- Bot API changelog —
  <https://core.telegram.org/bots/api-changelog>

Key versions: **Bot API 9.6** (2026-04-03) — Managed Bots (`getManagedBotToken`,
`replaceManagedBotToken`, `ManagedBotUpdated`); **Bot API 10.0** (2026-05-08) —
bot-to-bot messaging + guest mode; latest **Bot API 10.1** (2026-06-11). Verified
2026-07-04.
