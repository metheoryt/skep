---
name: gortex-tests-1-dirs
description: "Work in the tests +1 dirs area — 19 symbols across 3 files (77% cohesion)"
---

# tests +1 dirs

19 symbols | 3 files | 77% cohesion

## When to Use

Use this skill when working on files in:
- `external-call::dep:fleetd.agent.AgentProcess`
- `tests/test_agent.py`
- `tests/test_supervisor.py`

## Key Files

| File | Symbols |
|------|---------|
| `external-call::dep:fleetd.agent.AgentProcess` | fleetd.agent.AgentProcess |
| `tests/test_agent.py` | test_agent_streams_events_until_exit, tmp_path, test_agent_kill_stops_process, fake_claude_cmd, tmp_path, ... |
| `tests/test_supervisor.py` | events, FakeAgent, kill, tmp_path, __init__, ... |

## Entry Points

- `tests/test_supervisor.py::test_kill_unknown_returns_false`
- `tests/test_agent.py::test_agent_kill_stops_process`
- `tests/test_agent.py::test_agent_streams_events_until_exit`

## Connected Communities

- **. +2 dirs · fleetd.stream.Event** (2 cross-edges)
- **. +2 dirs · fleetd.db.Registry** (1 cross-edges)
- **. +2 dirs · MagicMock** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-41"
smart_context with task: "understand tests +1 dirs", format: "gcx"
find_usages with id: "tests/test_supervisor.py::test_kill_unknown_returns_false", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
