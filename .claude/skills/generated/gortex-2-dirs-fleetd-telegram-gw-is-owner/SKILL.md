---
name: gortex-2-dirs-fleetd-telegram-gw-is-owner
description: "Work in the . +2 dirs · fleetd.telegram_gw.is_owner area — 8 symbols across 3 files (85% cohesion)"
---

# . +2 dirs · fleetd.telegram_gw.is_owner

8 symbols | 3 files | 85% cohesion

## When to Use

Use this skill when working on files in:
- `external-call::dep:fleetd.telegram_gw.is_owner`
- `src/fleetd/app.py`
- `tests/test_telegram_gw.py`

## Key Files

| File | Symbols |
|------|---------|
| `external-call::dep:fleetd.telegram_gw.is_owner` | fleetd.telegram_gw.is_owner |
| `src/fleetd/app.py` | middleware, build_owner_middleware, data, event, config, ... |
| `tests/test_telegram_gw.py` | test_is_owner |

## Entry Points

- `tests/test_telegram_gw.py::test_is_owner`

## How to Explore

```
get_communities with id: "community-46"
smart_context with task: "understand . +2 dirs · fleetd.telegram_gw.is_owner", format: "gcx"
find_usages with id: "tests/test_telegram_gw.py::test_is_owner", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
