#!/usr/bin/env bash
# L1.1 permission probe, part 2 — the READ axis (and Edit).
#
# probe.sh tested Bash/Write/MCP. It never tested Read/Grep/Glob, so the
# "enumerate completely" strategy was resting on an incomplete enumeration.
#
# Q5: with the §2.3 grant, do Edit and Read fire?
# Q6: with ONLY an mcp grant, does Read still fire?
#     fires  => read tools are default-available; enumeration need not name them
#     denied => Read/Grep/Glob must join §5's grant
#
# Read evidence is out-of-band in the only way a read can be: the file holds an
# UNGUESSABLE token. An agent that echoes it must have read it.

set -uo pipefail
SCRATCH="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="/home/me/my/skep/.venv/bin/python"
STUB="$SCRATCH/stub_server.py"
RUNS="$SCRATCH/runs2"
TOKEN="ZQ7X-KESTREL-4417"
rm -rf "$RUNS"; mkdir -p "$RUNS"

run_scenario() {
  local name="$1"; shift
  local prompt="$1"; shift
  local dir="$RUNS/$name"
  local cwd="$dir/cwd"
  mkdir -p "$cwd/.claude" "$cwd/haystack"

  echo '{}' > "$cwd/.claude/settings.json"          # empty settings, as a fresh worktree has
  printf 'first-line %s\nsecond line\n' "$TOKEN" > "$cwd/target.txt"
  printf 'nothing here\n' > "$cwd/haystack/a.txt"
  printf 'needle %s\n' "$TOKEN" > "$cwd/haystack/b.txt"

  local mcpcfg
  mcpcfg=$(printf '{"mcpServers":{"stub":{"type":"stdio","command":"%s","args":["%s","%s"]}}}' \
            "$VENV_PY" "$STUB" "$cwd")

  echo "=== $name (flags: $*)"
  ( cd "$cwd" && timeout 240 claude -p "$prompt" \
      --mcp-config "$mcpcfg" --strict-mcp-config \
      --output-format text "$@" \
  ) > "$dir/stdout.txt" 2> "$dir/stderr.txt"
  echo "  exit=$?"

  if grep -qiE "not logged in|invalid api key" "$dir/stdout.txt" "$dir/stderr.txt" 2>/dev/null; then
    echo "  !! INVALID: spawn failed before the permission system."; echo; return
  fi

  # READ: the token is unguessable, so echoing it proves a read happened.
  if grep -q "$TOKEN" "$dir/stdout.txt"; then echo "  [FIRED]   Read  (token echoed)"
  else echo "  [denied]  Read  (token absent from output)"; fi

  # EDIT: the file on disk actually changed.
  if grep -q "EDITED" "$cwd/target.txt" 2>/dev/null; then echo "  [FIRED]   Edit  (target.txt mutated)"
  else echo "  [denied]  Edit  (target.txt unchanged)"; fi

  # GREP: naming the needle file requires a search (weaker: Read could substitute).
  if grep -qi "b\.txt" "$dir/stdout.txt"; then echo "  [FIRED?]  Grep  (named haystack/b.txt)"
  else echo "  [denied?] Grep  (did not name b.txt)"; fi
  echo
}

P5='Do these three things, then reply DONE:
1. Read the file target.txt and quote its first line verbatim in your reply.
2. Edit target.txt: replace the word "second" with "EDITED".
3. Use Grep to find which file under haystack/ contains the word "needle", and name it.
If a tool is denied, do not retry — say so and continue.'

P6='Do this one thing, then reply DONE:
1. Read the file target.txt and quote its first line verbatim in your reply.
If the tool is denied, do not retry — say exactly which tool was denied and why.'

# 5. The full §2.3 grant. Confirms Edit (granted-but-unprobed) and Read.
run_scenario "5-full-grant-read-edit-grep" "$P5" --allowedTools "Bash,Edit,Write,mcp__stub__ping"

# 6. THE DISCRIMINATOR: only an mcp grant. Does Read survive an argv --allowedTools
#    that does not name it? Answers whether read tools need enumerating.
run_scenario "6-read-with-mcp-grant-only" "$P6" --allowedTools "mcp__stub__ping"

echo "Interpretation:"
echo "  #5 Read+Edit FIRED  => §2.3 grant is sufficient for coding work"
echo "  #6 Read FIRED       => read tools default-available; --allowedTools is additive"
echo "  #6 Read denied      => Read,Grep,Glob MUST be added to §5's enumeration"
