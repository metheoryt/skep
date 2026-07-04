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
  `ANTHROPIC_BASE_URL` — relevant if auth ever changes. **Revisit before Phase
  2/3**, since Remote Control already covers sandboxing + much of the talk-back
  surface.

## Gotchas

- **`claude -p … --input-format stream-json` BLOCKS on stdin until EOF.** In
  headless one-shot mode the CLI reads its task as stream-json user messages from
  stdin and hangs forever if stdin is an open pipe that's never written/closed.
  Verified live 2026-07-04: with `--input-format stream-json` the process emitted
  0 events and never exited; without it, `claude -p "<task>" --output-format
  stream-json --verbose` streams a result and exits rc=0. **Phase 1 deliberately
  omits `--input-format` and uses `stdin=DEVNULL`** (see `agent.py._argv` /
  `start`). Phase 2 soft-steer must reintroduce `--input-format stream-json`
  *and* actually write a stream-json user message to stdin + keep the pipe
  managed — don't naively re-add the flag.

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
- Phase 1 is `native` mode only; sandbox, `ask_human` MCP, soft-steer, gated-ops
  approval, resume-after-restart, worktree cleanup are Phase 2/3 (see
  `docs/superpowers/specs/2026-07-04-agent-fleet-control-plane-design.md` §7).
