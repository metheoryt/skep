# skep

Per-machine supervisor daemon that dispatches headless Claude Code agents from
Telegram. See `docs/superpowers/specs/` for the design.

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
   ```

5. `uv run skep`

## Develop

`uv run pytest`
