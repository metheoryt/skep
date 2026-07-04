# Agent Fleet Control Plane — Design

**Date:** 2026-07-04
**Status:** Approved design, pre-implementation
**Repo:** `fleetd` (this repo) — cross-platform daemon. Deployed on NixOS via the
`~/gh/nix` flake; installed on Windows via its own service wrapper.

## 1. Purpose

A control plane for driving headless Claude Code agents from Telegram. Dispatch
a task to a repo from your phone, watch it work live, talk to it, steer or kill
it — all through a Telegram bot backed by a per-machine supervisor daemon
(`fleetd`).

The value proposition: headless Claude Code inherits your *existing* machine
setup (skills, gortex MCP, hooks, per-profile settings, credentials, real repos
and toolchains), so `fleetd` never re-implements the agent. It spawns, pipes,
formats, and routes.

### Non-goals (this version)

- **Cross-agent communication.** v1: you are the hub; all messages flow through
  you. No agent→agent bus. (Deferred.)
- **Cross-machine fleet.** One `fleetd` per machine, each managing that
  machine's agents. Unifying multiple machines under one bot is deferred
  (`getUpdates` is single-consumer; needs one-bot-per-machine or a central
  poller).

## 2. Design decisions (settled)

| Decision | Choice | Rationale |
|---|---|---|
| Core purpose | Full remote control plane (dispatch + monitor + talk + kill) | Telegram is where work *starts*, not just a viewer |
| Agent runtime | Headless Claude Code (`claude -p --output-format stream-json --input-format stream-json`) | Reuses existing skills/gortex/hooks/settings; least new code |
| Cross-agent comms | None in v1 — you are the hub | YAGNI; defer the message bus |
| Autonomy | Default-autonomous, ping on need; **irreversible ops gated** | Matches "agents do it themselves"; brakes only where it matters |
| Isolation | Per-task `native` \| `sandbox` (container) | Trusted tasks run on host; sensitive ones confined |
| Topic model | Topic-per-task, deleted on completion | Clean; no pile-up (`deleteForumTopic`) |
| Streaming UI | Live-edited "current activity" message | Avoids message spam |
| Daemon location | Dedicated cross-platform repo (`fleetd`) | NixOS flake can't deploy to Windows; distinct product |
| Packaging | Native process (systemd user unit / Windows service). **Not** Docker Compose | Containerizing agents fights environment reuse; Nix already gives reproducibility |

## 3. Architecture

### 3.1 Components

**1. `fleetd` — supervisor daemon.** Python + asyncio + SQLite. One instance per
machine, long-running.
- **Registry (SQLite):** each task = `{id, repo, worktree_path, session_id, pid,
  topic_id, mode, status, created_at}`. Survives daemon restart → sessions
  resume via `claude --resume <session_id>`.
- **Process manager:** per task, spawns headless Claude Code with cwd set to a
  fresh `git worktree`, holding its stdin/stdout pipes. Handles SIGTERM (kill)
  and soft-steer (write a user message to stdin).
- **Event router:** parses the stream-json event feed → formats → posts to the
  task's Telegram topic. Throttled to Telegram's edit limits (~1 edit/sec/chat).
- **Telegram gateway:** long-polling (`getUpdates`) — no public webhook, no
  inbound port, works behind NAT. Routes your replies into the right session's
  stdin, dispatches `#control` commands.
- **Audit log:** every dispatch and every command recorded in SQLite.

**2. Telegram bot + one forum supergroup (Topics enabled).**
- **`#control` topic:** fleet commands — `/spawn <repo> [--sandbox] <task>`,
  `/ls`, `/kill <id>`, `/panic`, a live status board.
- **One topic per task** (e.g. `nix · refactor-nvidia`): created on spawn,
  `deleteForumTopic` on completion. Carries both the conversational stream and
  the log. A single **live-edited "current activity" message** streams verbose
  tool-by-tool activity (no spam); **durable messages** mark milestones (file
  written, tests passed, question asked) and a done-summary with inline
  **[interrupt] [kill] [close]** buttons.
- Bot requires `can_manage_topics` admin right to create/delete topics.

**3. `human-loop` MCP server.** Small MCP server handed to each spawned agent via
`--mcp-config`. The "talk to them" channel:
- `ask_human(question)` — **blocks** the tool call, posts the question to the
  agent's topic, returns your reply as the tool result. (Headless has no
  `AskUserQuestion`; this is the human-in-the-loop primitive.)
- `notify_human(msg)` — fire-and-forget status ping.
- Backs the `--permission-prompt-tool` for gated-ops approval (see §4.2).
- `Notification` and `Stop` hooks POST lifecycle events to `fleetd`.

**4. Formatting layer.** stream-json → Telegram MarkdownV2. Assistant text
streams into the live activity message; tool calls render compactly
(`🔧 edit_file nvidia.nix`); done event posts a summary. Respects rate limits.

### 3.2 Data flow (one task)

```
you    →  #control: /spawn nix "clean up nvidia power mgmt"
fleetd →  git worktree add …  ·  create topic "nix · nvidia"  ·  spawn claude -p
agent  →  stream-json events   →  fleetd formats  →  live message in topic
agent  →  ask_human("keep fine-grained PM?")  →  posts to topic, blocks
you    →  reply in topic  →  fleetd → tool_result → agent continues
agent  →  wants: git push --force  →  PreToolUse classifier: irreversible
       →  fleetd posts [approve][deny], blocks
you    →  [approve]  →  agent proceeds
agent  →  Stop hook  →  fleetd posts summary + [kill][close] buttons
you    →  [close]    →  deleteForumTopic, mark done
```

## 4. Security & blast radius

**A dispatched agent runs as you** — same user, ssh keys, git push rights,
`docker` group (root-equivalent), Anthropic credits, and (per existing posture)
self-commits and ships on push. The control channel is therefore a remote
foothold with full privileges. This section is first-class, not an afterthought.

### 4.1 Authentication

- **Hard allowlist on the Telegram user ID.** `fleetd` ignores every update whose
  sender is not the owner. No open commands, ever.
- **Bot token in OS secret storage**, never committed. (Mirrors the pattern of
  keeping the work Sentry secret in per-repo `.claude/settings.local.json`, out
  of git.)
- **`/panic`** — fleet-wide SIGTERM of every agent.

### 4.2 Autonomy — default-autonomous with selective brakes

Implemented via `--permission-prompt-tool` (→ `human-loop`) backed by a
**PreToolUse hook** that classifies each action:
- **Reversible** (read, edit, commit, run tests, gortex) → auto-approved.
- **Irreversible** (force-push, push to a prod/deploy branch, `rm -rf`,
  `docker system prune`, destructive DB ops, `curl … | sh`) → routed to Telegram
  as a **blocking inline approve/deny**, exact command shown. Agent waits.
- The classifier is a **configurable ruleset** (patterns + branch/endpoint
  awareness), tuned over time.

### 4.3 Isolation — per-task mode

`/spawn` takes a mode flag:
- **`native`** (default) — full host access. Trusted tasks.
- **`sandbox`** — container-per-agent, confined to its worktree bind-mount, no
  host credentials/ssh, throwaway env. Sensitive/experimental tasks. Gated-ops
  approval (§4.2) still applies on top.

## 5. Interrupt model (honest constraints)

True instant mid-tool interrupt of a headless CLI is limited. Two real levers:
- **Soft steer** — reply in a topic → `fleetd` writes a user message into the
  running session's stdin; lands at the next turn/tool boundary.
- **Hard stop** — `[kill]` button / `/kill` → SIGTERM the process; session is
  resumable via `--resume` with a correction.

## 6. Tech stack

- **Language:** Python 3.13 + asyncio (fits the pipe-juggling; matches existing
  uv toolchain). Telegram: **`aiogram`** (long-polling). MCP: official Python
  MCP SDK for the `human-loop` server.
- **Persistence:** SQLite.
- **Packaging/deploy:**
  - Core: pure Python, no OS assumptions.
  - NixOS: `~/gh/nix` consumes this repo as a flake input; `modules/home/fleetd.nix`
    runs it as a **systemd user service** on g16 + latitude5520.
  - Windows: installed from this repo via its own service wrapper (Scheduled Task
    / `nssm`), mirroring the existing `git-autofetch` split.
- **Not containerized** (see §2 rationale). Docker stays in the back pocket only
  for the optional per-task sandbox mode (§4.3).

## 7. Phasing

**Phase 1 — Monitor + lifecycle + auth.**
`fleetd` core, spawn/`ls`/kill, per-task topics, stream-json → live-edited
formatting, Telegram user-ID lock, audit log, `/panic`. Native mode only.
*Outcome:* dispatch from `#control`, watch live, kill. Useful on day one.

**Phase 2 — Queen + isolated workers.** *(Re-scoped 2026-07-04 — see
`2026-07-04-fleetd-phase2-queen-workers-design.md`.)* Split the Phase-1 single
process into a Telegram-owning **queen** and one or more **workers**; multi-host,
mDNS + public-link + WG discovery, per-worker Claude-profile isolation, capacity
cap. The original "talk-back + brakes" scope moved to Phase 3.

**Phase 3 — Talk-back + brakes.**
`human-loop` MCP (`ask_human`, `notify_human`), reply-routing / soft-steer,
gated-ops approval — via a **blocking PreToolUse hook** (note: `--permission-prompt-tool`
was removed in `claude` 2.1.201).

**Phase 4 — Sandbox** (container-per-agent), plus polish: live status board,
worktree auto-lifecycle, resume-after-restart, formatting refinement.

**Later (out of scope for this spec).**
Cross-agent message bus; cross-machine fleet unification.

## 8. Open questions for implementation planning

- Exact `stream-json` event schema mapping → which events become live-edit
  updates vs durable milestone messages.
- Windows sandbox story (Docker Desktop vs WSL2) — Phase 2 detail.
- Worktree naming/placement convention and cleanup policy (Phase 3).
- Config format for the irreversible-ops classifier ruleset.
