---
name: gortex-1-dirs-fleetd-stream-parse-event
description: "Work in the . +1 dirs · fleetd.stream.parse_event area — 15 symbols across 3 files (94% cohesion)"
---

# . +1 dirs · fleetd.stream.parse_event

15 symbols | 3 files | 94% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `external-call::dep:fleetd.stream.parse_event`
- `tests/test_stream.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | dumps, json, loads |
| `external-call::dep:fleetd.stream.parse_event` | fleetd.stream.parse_event |
| `tests/test_stream.py` | test_assistant_text_block, test_user_tool_result_block, test_non_json_returns_none, test_unknown_type_is_unknown_kind, test_user_message_null_does_not_crash, ... |

## Entry Points

- `tests/test_stream.py::test_non_dict_json_returns_none`

## How to Explore

```
get_communities with id: "community-40"
smart_context with task: "understand . +1 dirs · fleetd.stream.parse_event", format: "gcx"
find_usages with id: "tests/test_stream.py::test_non_dict_json_returns_none", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
