---
name: gortex-2-dirs-path
description: "Work in the . +2 dirs · Path area — 20 symbols across 9 files (85% cohesion)"
---

# . +2 dirs · Path

20 symbols | 9 files | 85% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `external-call::dep:aiogram.Bot`
- `external-call::dep:aiogram.client.default.DefaultBotProperties`
- `external-call::dep:fleetd.config.load_config`
- `external-call::stdlib:pytest`
- `src/fleetd/config.py`
- `src/fleetd/telegram_gw.py`
- `tests/test_config.py`
- `tests/test_telegram_gw.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | pathlib.Path, Path |
| `external-call::dep:aiogram.Bot` | aiogram.Bot |
| `external-call::dep:aiogram.client.default.DefaultBotProperties` | aiogram.client.default.DefaultBotProperties |
| `external-call::dep:fleetd.config.load_config` | fleetd.config.load_config |
| `external-call::stdlib:pytest` | pytest |
| `src/fleetd/config.py` | env, Config, load_config |
| `src/fleetd/telegram_gw.py` | __init__, config, bot, build_bot, config |
| `tests/test_config.py` | test_load_config_claude_bin_override, test_load_config_missing_required_raises, test_config_is_frozen, test_load_config_parses_all_fields, _base_env |
| `tests/test_telegram_gw.py` | _cfg |

## Entry Points

- `src/fleetd/config.py::load_config`
- `tests/test_config.py::test_load_config_parses_all_fields`
- `tests/test_config.py::test_config_is_frozen`
- `tests/test_config.py::test_load_config_missing_required_raises`

## Connected Communities

- **. +2 dirs · MagicMock** (2 cross-edges)

## How to Explore

```
get_communities with id: "community-24"
smart_context with task: "understand . +2 dirs · Path", format: "gcx"
find_usages with id: "src/fleetd/config.py::load_config", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
