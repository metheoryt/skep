# skep — shared-memory proposals, 2026-09-12

Harvested from `/home/me/my/skep` at `9266874` (first harvest, Track B full pass;
zero transcript digests were partitioned to this repo, so Track A was skipped).
Lane 2 only — nothing below has been written. Each row stands alone; do not
expect a transcript to still exist.

Format: `tier | action | fact (exact text to paste) | target file | source | confidence`

## global

global | add | - **`gortex daemon reload` and `gortex daemon restart` do NOT purge already-indexed nodes for a path you just added to `.gortex.yaml`'s `exclude:` — both restore them from the snapshot.** Only `gortex untrack <repo>` followed by `gortex track <repo>` actually drops them. Measured on the `skep` repo 2026-07-25 while excluding a docs subtree; the node count did not move until the untrack/track cycle. Budget a full reindex when you change an exclude, and verify by node count rather than by assuming the reload took. | `~/.claude/memory/global.md` (under the gortex/tooling section) | skep `1c3a33b` (commit message, 2026-07-25) | high

global | add | - **A bare `gortex init` commits agent-visible artifacts, not just wiring.** It writes `.claude/skills/generated/gortex-*/SKILL.md` navigation blurbs AND injects a `## Community Skills` table into `CLAUDE.md`/`AGENTS.md`. Those blurbs are never indexed (`.claude/` is in gortex's builtin exclude list), so they cost nothing in the graph — their cost is that the injected table rides into every agent session's context and goes stale the moment a symbol is renamed, pointing agents at dead code. If you don't want them, gitignore `.claude/skills/generated/` and drop the table; a later `gortex init` by anyone then regenerates locally and commits nothing. | `~/.claude/memory/global.md` (under the gortex/tooling section) | skep `1c3a33b` (commit message + tree, 2026-07-25) | high

## host:<name>

(none — nothing host-specific surfaced in this pass. The one measurement taken on
g513ie, that `~/my/skep` carries no `.venv` and so `uvx ty check src` aborts on
its own config, is a repo-onboarding fact and was written to skep's own
`project.md` Gotchas instead.)

## personality

(none)
