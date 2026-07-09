# L1.1 permission probes

Evidence behind §2.4 and §2.5 of `../../2026-07-09-l1.1-agent-memory-files-design.md`.
Run 2026-07-09 against `claude` 2.1.205.

- `probe.sh` — scenarios 1–4: `Bash`, `Write`, and an MCP tool.
- `probe2.sh` — scenarios 5–6: the read axis, plus `Edit`.
- `stub_server.py` — a one-tool stdio MCP server (`ping`), stands in for `MemoryShim`.

Both scripts spawn real headless agents. They vary the settings-derived allowlist through
project-scope `<cwd>/.claude/settings.json`, never through `CLAUDE_CONFIG_DIR`, so no
credential is copied anywhere. `--strict-mcp-config` keeps the operator's own MCP servers out.

## Two methodological rules, both of which caught a false conclusion

**Evidence is out-of-band.** Each capability leaves a marker on disk (or, for a read, echoes
an unguessable token). The agent's self-report is never the evidence — though it is worth
reading, because it names the *mechanism* of a denial.

**Every treatment needs a control.** Scenario 1 grants `Bash` via settings and passes no
flag. It failed: the settings allowlist never armed. Without it, scenario 3 would have read
as a clean "`--allowedTools` REPLACES" verdict and sent the design somewhere the evidence
does not support.

Two conclusions were *withdrawn* on inspection, both by reading the denial reason rather
than the marker:

- `Bash` denials came from the **sandbox working-directory guard**, not the permission
  system — an artifact of nesting `claude` inside an already-sandboxed shell.
- Scenario 5's `Grep` marker was a **false positive**: `Grep` was not registered in the
  session at all, and the agent substituted `grep` via `Bash`.

Re-running requires the `mcp` package (`skep`'s `.venv` has it) and updates `VENV_PY` if the
path differs.
