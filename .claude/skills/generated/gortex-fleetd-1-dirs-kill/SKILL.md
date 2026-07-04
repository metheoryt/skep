---
name: gortex-fleetd-1-dirs-kill
description: "Work in the fleetd +1 dirs · kill area — 11 symbols across 3 files (59% cohesion)"
---

# fleetd +1 dirs · kill

11 symbols | 3 files | 59% cohesion

## When to Use

Use this skill when working on files in:
- `src/fleetd/app.py`
- `src/fleetd/supervisor.py`
- `tests/test_supervisor.py`

## Key Files

| File | Symbols |
|------|---------|
| `src/fleetd/app.py` | _panic, message, command, message, _kill |
| `src/fleetd/supervisor.py` | Supervisor, kill, panic, task_id |
| `tests/test_supervisor.py` | test_panic_kills_all_active, tmp_path |

## Entry Points

- `tests/test_supervisor.py::test_panic_kills_all_active`

## Connected Communities

- **fleetd +1 dirs · run_events** (3 cross-edges)
- **. +2 dirs · fleetd.stream.Event** (2 cross-edges)
- **. +2 dirs · fleetd.db.Registry** (1 cross-edges)
- **. +2 dirs · MagicMock** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-28"
smart_context with task: "understand fleetd +1 dirs · kill", format: "gcx"
find_usages with id: "tests/test_supervisor.py::test_panic_kills_all_active", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
