---
name: gortex-fleetd-task
description: "Work in the fleetd · Task area — 12 symbols across 2 files (72% cohesion)"
---

# fleetd · Task

12 symbols | 2 files | 72% cohesion

## When to Use

Use this skill when working on files in:
- `src/fleetd/db.py`
- `src/fleetd/supervisor.py`

## Key Files

| File | Symbols |
|------|---------|
| `src/fleetd/db.py` | list_active, row, list_all, Task, _row_to_task |
| `src/fleetd/supervisor.py` | agent_factory, registry, config, list_active, __init__, ... |

## Entry Points

- `src/fleetd/db.py::Registry.list_active`
- `src/fleetd/db.py::Registry.list_all`

## How to Explore

```
get_communities with id: "community-25"
smart_context with task: "understand fleetd · Task", format: "gcx"
find_usages with id: "src/fleetd/db.py::Registry.list_active", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
