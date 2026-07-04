---
name: gortex-fleetd-activity-line
description: "Work in the fleetd · activity_line area — 8 symbols across 1 files (95% cohesion)"
---

# fleetd · activity_line

8 symbols | 1 files | 95% cohesion

## When to Use

Use this skill when working on files in:
- `src/fleetd/formatting.py`

## Key Files

| File | Symbols |
|------|---------|
| `src/fleetd/formatting.py` | text, escape_md, activity_line, milestone_message, event, ... |

## Entry Points

- `src/fleetd/formatting.py::activity_line`

## How to Explore

```
get_communities with id: "community-27"
smart_context with task: "understand fleetd · activity_line", format: "gcx"
find_usages with id: "src/fleetd/formatting.py::activity_line", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
