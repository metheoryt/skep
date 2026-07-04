---
name: gortex-2-dirs-magicmock
description: "Work in the . +2 dirs · MagicMock area — 24 symbols across 9 files (77% cohesion)"
---

# . +2 dirs · MagicMock

24 symbols | 9 files | 77% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `external-call::dep:aiogram.exceptions.TelegramBadRequest`
- `external-call::dep:fleetd.app.build_owner_middleware`
- `external-call::dep:fleetd.config.Config`
- `external-call::dep:fleetd.telegram_gw.Gateway`
- `src/fleetd/telegram_gw.py`
- `tests/test_integration.py`
- `tests/test_supervisor.py`
- `tests/test_telegram_gw.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | MagicMock, unittest.mock.AsyncMock, AsyncMock, unittest.mock.MagicMock |
| `external-call::dep:aiogram.exceptions.TelegramBadRequest` | aiogram.exceptions.TelegramBadRequest |
| `external-call::dep:fleetd.app.build_owner_middleware` | fleetd.app.build_owner_middleware |
| `external-call::dep:fleetd.config.Config` | fleetd.config.Config |
| `external-call::dep:fleetd.telegram_gw.Gateway` | fleetd.telegram_gw.Gateway |
| `src/fleetd/telegram_gw.py` | delete_topic, Gateway, topic_id |
| `tests/test_integration.py` | tmp_path, test_owner_middleware_blocks_non_owner, tmp_path, tmp_path, test_owner_middleware_passes_owner, ... |
| `tests/test_supervisor.py` | tmp_path, _cfg |
| `tests/test_telegram_gw.py` | test_delete_topic_calls_bot, test_create_topic_returns_thread_id, test_post_returns_message_id, test_edit_swallows_not_modified |

## Entry Points

- `tests/test_integration.py::test_owner_middleware_passes_owner`
- `tests/test_integration.py::test_owner_middleware_blocks_non_owner`
- `tests/test_telegram_gw.py::test_create_topic_returns_thread_id`
- `tests/test_telegram_gw.py::test_edit_swallows_not_modified`
- `tests/test_telegram_gw.py::test_post_returns_message_id`

## How to Explore

```
get_communities with id: "community-39"
smart_context with task: "understand . +2 dirs · MagicMock", format: "gcx"
find_usages with id: "tests/test_integration.py::test_owner_middleware_passes_owner", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
