---
name: gortex-2-dirs-fleetd-db-registry
description: "Work in the . +2 dirs · fleetd.db.Registry area — 26 symbols across 7 files (61% cohesion)"
---

# . +2 dirs · fleetd.db.Registry

26 symbols | 7 files | 61% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `external-call::dep:fleetd.app.format_ls`
- `external-call::dep:fleetd.db.Registry`
- `src/fleetd/db.py`
- `tests/test_db.py`
- `tests/test_integration.py`
- `tests/test_supervisor.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | sleep |
| `external-call::dep:fleetd.app.format_ls` | fleetd.app.format_ls |
| `external-call::dep:fleetd.db.Registry` | fleetd.db.Registry |
| `src/fleetd/db.py` | close, conn, audit_rows, __init__, Registry |
| `tests/test_db.py` | test_update_fields, test_add_and_get_task, test_audit_log_persists_rows, test_list_active_excludes_terminal, test_get_missing_returns_none |
| `tests/test_integration.py` | test_format_ls_lists_tasks, test_format_ls_escapes_markdownv2, test_end_to_end_spawn_with_fake_claude, git_repo, fake_claude_cmd, ... |
| `tests/test_supervisor.py` | tmp_path, test_spawn_worktree_failure_marks_failed_and_raises, worktree_path, wt_factory, branch, ... |

## Entry Points

- `tests/test_supervisor.py::test_spawn_worktree_failure_marks_failed_and_raises`
- `tests/test_integration.py::test_end_to_end_spawn_with_fake_claude`
- `tests/test_db.py::test_list_active_excludes_terminal`
- `tests/test_integration.py::test_format_ls_lists_tasks`
- `tests/test_integration.py::test_format_ls_escapes_markdownv2`

## Connected Communities

- **. +2 dirs · fleetd.stream.Event** (4 cross-edges)
- **. +2 dirs · MagicMock** (2 cross-edges)
- **. +2 dirs · Path** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-37"
smart_context with task: "understand . +2 dirs · fleetd.db.Registry", format: "gcx"
find_usages with id: "tests/test_supervisor.py::test_spawn_worktree_failure_marks_failed_and_raises", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
