# skep

Per-machine supervisor daemon that dispatches headless Claude Code agents from
Telegram.

**New here? Read [`ARCHITECTURE.md`](ARCHITECTURE.md)** — what skep is, how a
Telegram message becomes a running agent, and a glossary of the vocabulary
(`queen`, `worker`, the L0–L5 ladder). The specs under `docs/superpowers/specs/`
record *why* each decision was made, on the day it was made; some are superseded.

## Setup

1. Create a Telegram bot via @BotFather; note the token.
2. Create a supergroup, enable **Topics**, add the bot as admin with the
   **Manage Topics** right.
3. Get your own Telegram user ID (e.g. via @userinfobot) and the group chat ID.
4. Export config (never commit these):

   ```sh
   export SKEP_BOT_TOKEN=...        # from BotFather
   export SKEP_OWNER_ID=123456789   # your user ID; only you may command the bot
   export SKEP_GROUP_CHAT_ID=-100...# the supergroup chat ID
   export SKEP_REPOS_ROOT=$HOME/gh  # where your repos live
   export SKEP_WORKTREES_ROOT=$HOME/.skep/worktrees
   # optional: SKEP_CLAUDE_BIN=claude
   # optional: SKEP_MEMORY_ENABLED=false   # turn off the agent-memory addendum
   ```

5. `uv run skep`

## Agent memory

Agents get durable, per-repo memory through the machine's
[gortex](https://github.com/gortexhq/gortex) daemon — skep stores nothing. At
spawn, each agent's system prompt gains a short addendum telling it to
`gortex memory recall --index <repo>` before starting and to store operational
facts it learns.

This requires a running gortex daemon on the worker host that **tracks each repo
agents work in**. The worker smoke-checks the exact recall command once per repo;
if gortex is missing, the daemon is down or wedged, or the repo is untracked, the
addendum is omitted, a warning is logged, and agents run normally without memory.
Set `SKEP_MEMORY_ENABLED=false` to omit it unconditionally.

Memory is scoped to the repo's gortex workspace and is shared by every agent that
works on that repo. Profiles are isolated only because personal and work profiles
live on separate hosts with separate daemons — co-locating them would share a
repo's memory across profiles. See
`docs/superpowers/specs/2026-07-09-l1-memory-substrate-design.md` §4.1.

## Develop

`uv run pytest`
