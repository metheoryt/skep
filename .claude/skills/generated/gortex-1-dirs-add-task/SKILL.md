---
name: gortex-1-dirs-add-task
description: "Work in the . +1 dirs · add_task area — 9 symbols across 2 files (84% cohesion)"
---

# . +1 dirs · add_task

9 symbols | 2 files | 84% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `src/fleetd/db.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | now, datetime.datetime, isoformat |
| `src/fleetd/db.py` | _now, add_task, mode, task, repo, ... |

## How to Explore

```
get_communities with id: "community-26"
smart_context with task: "understand . +1 dirs · add_task", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
