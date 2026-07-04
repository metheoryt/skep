---
name: gortex-1-dirs-main-external-call-dep-fleetd-telegram-gw
description: "Work in the . +1 dirs · main · . · external-call::dep:fleetd.telegram_gw area — 6 symbols across 3 files (58% cohesion)"
---

# . +1 dirs · main · . · external-call::dep:fleetd.telegram_gw

6 symbols | 3 files | 58% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `external-call::dep:fleetd.telegram_gw.build_bot`
- `src/fleetd/app.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | run, os, get |
| `external-call::dep:fleetd.telegram_gw.build_bot` | fleetd.telegram_gw.build_bot |
| `src/fleetd/app.py` | run, main |

## Entry Points

- `src/fleetd/app.py::main`
- `src/fleetd/app.py::run`

## Connected Communities

- **. +2 dirs · Path** (1 cross-edges)
- **. +2 dirs · fleetd.db.Registry** (1 cross-edges)
- **. +2 dirs · fleetd.stream.Event** (1 cross-edges)
- **. +2 dirs · MagicMock** (1 cross-edges)
- **. +2 dirs · build_dispatcher** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-23"
smart_context with task: "understand . +1 dirs · main · . · external-call::dep:fleetd.telegram_gw", format: "gcx"
find_usages with id: "src/fleetd/app.py::main", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
