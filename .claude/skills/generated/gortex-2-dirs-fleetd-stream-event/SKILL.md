---
name: gortex-2-dirs-fleetd-stream-event
description: "Work in the . +2 dirs · fleetd.stream.Event area — 35 symbols across 7 files (72% cohesion)"
---

# . +2 dirs · fleetd.stream.Event

35 symbols | 7 files | 72% cohesion

## When to Use

Use this skill when working on files in:
- `external-call::dep:fleetd.formatting.activity_line`
- `external-call::dep:fleetd.formatting.milestone_message`
- `external-call::dep:fleetd.stream.Event`
- `external-call::dep:fleetd.supervisor.Supervisor`
- `src/fleetd/stream.py`
- `tests/test_formatting.py`
- `tests/test_supervisor.py`

## Key Files

| File | Symbols |
|------|---------|
| `external-call::dep:fleetd.formatting.activity_line` | fleetd.formatting.activity_line |
| `external-call::dep:fleetd.formatting.milestone_message` | fleetd.formatting.milestone_message |
| `external-call::dep:fleetd.stream.Event` | fleetd.stream.Event |
| `external-call::dep:fleetd.supervisor.Supervisor` | fleetd.supervisor.Supervisor |
| `src/fleetd/stream.py` | obj, Event, _first_blocks, line, parse_event |
| `tests/test_formatting.py` | test_milestone_for_error_result, test_activity_line_truncates_long_text, test_activity_line_none_for_tool_result, test_milestone_none_for_assistant_text, test_activity_line_for_tool_use, ... |
| `tests/test_supervisor.py` | tmp_path, test_run_events_edits_activity_and_marks_done, test_run_events_gateway_exception_marks_failed, tmp_path, test_run_events_activity_posts_once_then_edits, ... |

## Entry Points

- `tests/test_supervisor.py::test_run_events_gateway_exception_marks_failed`
- `tests/test_supervisor.py::test_run_events_edits_activity_and_marks_done`
- `tests/test_supervisor.py::test_run_events_killed_no_result_no_error_audit`
- `tests/test_supervisor.py::test_run_events_preserves_killed_status`
- `tests/test_supervisor.py::test_run_events_activity_posts_once_then_edits`

## Connected Communities

- **. +2 dirs · MagicMock** (14 cross-edges)
- **. +2 dirs · fleetd.db.Registry** (8 cross-edges)
- **. +1 dirs · fleetd.stream.parse_event** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-44"
smart_context with task: "understand . +2 dirs · fleetd.stream.Event", format: "gcx"
find_usages with id: "tests/test_supervisor.py::test_run_events_gateway_exception_marks_failed", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
