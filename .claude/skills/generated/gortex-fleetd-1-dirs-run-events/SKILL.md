---
name: gortex-fleetd-1-dirs-run-events
description: "Work in the fleetd +1 dirs · run_events area — 32 symbols across 6 files (72% cohesion)"
---

# fleetd +1 dirs · run_events

32 symbols | 6 files | 72% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `src/fleetd/agent.py`
- `src/fleetd/app.py`
- `src/fleetd/db.py`
- `src/fleetd/supervisor.py`
- `src/fleetd/telegram_gw.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | sub, strip, re |
| `src/fleetd/agent.py` | events |
| `src/fleetd/app.py` | _spawn, command, message |
| `src/fleetd/db.py` | fields, detail, get_task, kind, update, ... |
| `src/fleetd/supervisor.py` | task_id, text, spawn, task_text, run_events, ... |
| `src/fleetd/telegram_gw.py` | post, topic_id, edit, text, create_topic, ... |

## Entry Points

- `src/fleetd/supervisor.py::Supervisor.run_events`
- `src/fleetd/supervisor.py::Supervisor.spawn`

## Connected Communities

- **. +1 dirs · add_task** (2 cross-edges)
- **. +2 dirs · fleetd.stream.Event** (2 cross-edges)
- **fleetd +1 dirs · start** (2 cross-edges)
- **. +1 dirs · fleetd.stream.parse_event** (1 cross-edges)
- **fleetd · Task** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-29"
smart_context with task: "understand fleetd +1 dirs · run_events", format: "gcx"
find_usages with id: "src/fleetd/supervisor.py::Supervisor.run_events", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
