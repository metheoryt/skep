---
name: gortex-fleetd-1-dirs-start
description: "Work in the fleetd +1 dirs · start area — 17 symbols across 3 files (82% cohesion)"
---

# fleetd +1 dirs · start

17 symbols | 3 files | 82% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `src/fleetd/agent.py`
- `src/fleetd/supervisor.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | create_subprocess_exec, asyncio, create_task, shlex, split, ... |
| `src/fleetd/agent.py` | claude_bin, _drain_stderr, kill, _argv, returncode, ... |
| `src/fleetd/supervisor.py` | agent |

## Entry Points

- `src/fleetd/agent.py::AgentProcess.start`

## How to Explore

```
get_communities with id: "community-31"
smart_context with task: "understand fleetd +1 dirs · start", format: "gcx"
find_usages with id: "src/fleetd/agent.py::AgentProcess.start", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
