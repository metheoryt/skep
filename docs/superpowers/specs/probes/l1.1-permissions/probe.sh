#!/usr/bin/env bash
# L1.1 permission probe (spec §2.2, §2.3).
#
# Question 1 (§2.3): can skep grant a working coding baseline purely on argv,
#                    against a profile with no permissions block?
# Question 2 (§2.2): does --allowedTools MERGE with a settings allowlist, or REPLACE it?
#
# Evidence is out-of-band: each capability leaves a marker file. The agent's
# self-report is ignored.
#
# No credentials are copied and CLAUDE_CONFIG_DIR is not overridden: the spawns
# use the normal profile for auth. The settings-derived allowlist is varied via
# PROJECT-scope <cwd>/.claude/settings.json, which the harness reads natively.
# ~/.claude/settings.json has no `permissions` block, so the baseline is empty.
# --strict-mcp-config keeps the personal MCP servers out.

set -uo pipefail
SCRATCH="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="/home/me/my/skep/.venv/bin/python"
STUB="$SCRATCH/stub_server.py"
RUNS="$SCRATCH/runs"
rm -rf "$RUNS"; mkdir -p "$RUNS"

PROMPT='Do these three things in order, then reply DONE:
1. Use the Bash tool to run exactly: touch bash-marker
2. Use the Write tool to create a file named write-marker containing: ok
3. Call the MCP tool named ping
If a tool is denied, do not retry it — skip it and continue to the next step.'

# $1 = scenario name, $2 = settings mode (empty|bash), $3... = extra claude flags
run_scenario() {
  local name="$1"; shift
  local settings="$1"; shift
  local dir="$RUNS/$name"
  local cwd="$dir/cwd"
  mkdir -p "$cwd/.claude"

  if [[ "$settings" == "bash" ]]; then
    echo '{"permissions":{"allow":["Bash"]}}' > "$cwd/.claude/settings.json"
  else
    echo '{}' > "$cwd/.claude/settings.json"
  fi

  local mcpcfg
  mcpcfg=$(printf '{"mcpServers":{"stub":{"type":"stdio","command":"%s","args":["%s","%s"]}}}' \
            "$VENV_PY" "$STUB" "$cwd")

  echo "=== $name (settings: $settings, flags: $*)"
  ( cd "$cwd" && timeout 180 claude -p "$PROMPT" \
      --mcp-config "$mcpcfg" --strict-mcp-config \
      --output-format text "$@" \
  ) > "$dir/stdout.txt" 2> "$dir/stderr.txt"
  echo "  exit=$?"

  # A spawn that never reached the permission system must not read as "denied".
  if grep -qiE "not logged in|invalid api key|authentication" "$dir/stdout.txt" "$dir/stderr.txt" 2>/dev/null; then
    echo "  !! INVALID: spawn failed before the permission system. Markers mean nothing."
    echo; return
  fi

  # Filesystem evidence, not self-report.
  for m in bash-marker write-marker ping-marker; do
    if [[ -e "$cwd/$m" ]]; then echo "  [FIRED]   $m"; else echo "  [denied]  $m"; fi
  done
  echo
}

# 1. Control: settings grant Bash, no --allowedTools at all.
#    If bash-marker does NOT fire here, scenario 3 is uninterpretable.
run_scenario "1-control-settings-bash-only" bash

# 2. §2.3: enumerate every axis on argv against empty settings.
#    All three firing => skep can grant its own baseline; merge semantics moot.
run_scenario "2-argv-enumerate-all" empty --allowedTools "Bash,Write,mcp__stub__ping"

# 3. §2.2 discriminator: settings grant Bash, argv adds ONLY the mcp tool.
#    bash-marker fires => MERGE.  bash-marker denied => REPLACE.
run_scenario "3-merge-vs-replace" bash --allowedTools "mcp__stub__ping"

# 4. Confirms empty baseline: the mcp grant alone must not smuggle in Bash/Write.
run_scenario "4-mcp-only-empty-settings" empty --allowedTools "mcp__stub__ping"

echo "Interpretation:"
echo "  #1 bash FIRED           => control valid, settings allowlist works"
echo "  #2 all three FIRED      => argv-only baseline works (answers §2.3)"
echo "  #3 bash FIRED           => --allowedTools MERGES"
echo "  #3 bash denied (w/ #1)  => --allowedTools REPLACES (§5 must enumerate all tools)"
echo "  #4 only ping FIRED      => baseline is genuinely empty"
