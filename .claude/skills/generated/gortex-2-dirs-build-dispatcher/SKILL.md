---
name: gortex-2-dirs-build-dispatcher
description: "Work in the . +2 dirs · build_dispatcher area — 13 symbols across 6 files (80% cohesion)"
---

# . +2 dirs · build_dispatcher

13 symbols | 6 files | 80% cohesion

## When to Use

Use this skill when working on files in:
- `external-call::dep:aiogram.Dispatcher`
- `external-call::dep:aiogram.F`
- `external-call::dep:aiogram.filters.Command`
- `external-call::dep:fleetd.formatting.escape_md`
- `src/fleetd/app.py`
- `tests/test_formatting.py`

## Key Files

| File | Symbols |
|------|---------|
| `external-call::dep:aiogram.Dispatcher` | aiogram.Dispatcher |
| `external-call::dep:aiogram.F` | aiogram.F |
| `external-call::dep:aiogram.filters.Command` | aiogram.filters.Command |
| `external-call::dep:fleetd.formatting.escape_md` | fleetd.formatting.escape_md |
| `src/fleetd/app.py` | config, format_ls, message, owner_only, supervisor, ... |
| `tests/test_formatting.py` | test_escape_md_escapes_backslash, test_escape_md_escapes_reserved_chars |

## Entry Points

- `src/fleetd/app.py::build_dispatcher`

## Connected Communities

- **. +2 dirs · fleetd.telegram_gw.is_owner** (2 cross-edges)
- **fleetd +1 dirs · kill** (2 cross-edges)
- **fleetd · Task** (1 cross-edges)
- **fleetd +1 dirs · run_events** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-38"
smart_context with task: "understand . +2 dirs · build_dispatcher", format: "gcx"
find_usages with id: "src/fleetd/app.py::build_dispatcher", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
