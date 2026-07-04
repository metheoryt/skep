---
name: gortex-1-dirs-main-fake-claude
description: "Work in the . +1 dirs · main · . · fake_claude area — 6 symbols across 2 files (91% cohesion)"
---

# . +1 dirs · main · . · fake_claude

6 symbols | 2 files | 91% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `tests/fixtures/fake_claude.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | time, write, sleep, flush, sys |
| `tests/fixtures/fake_claude.py` | main |

## Entry Points

- `tests/fixtures/fake_claude.py::main`

## Connected Communities

- **. +1 dirs · fleetd.stream.parse_event** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-35"
smart_context with task: "understand . +1 dirs · main · . · fake_claude", format: "gcx"
find_usages with id: "tests/fixtures/fake_claude.py::main", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
