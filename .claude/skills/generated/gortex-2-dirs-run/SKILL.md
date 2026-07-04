---
name: gortex-2-dirs-run
description: "Work in the . +2 dirs · run area — 8 symbols across 3 files (100% cohesion)"
---

# . +2 dirs · run

8 symbols | 3 files | 100% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `src/fleetd/agent.py`
- `tests/conftest.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | subprocess, run |
| `src/fleetd/agent.py` | branch, worktree_path, repo_path, create_worktree |
| `tests/conftest.py` | tmp_path, git_repo |

## Entry Points

- `tests/conftest.py::git_repo`
- `src/fleetd/agent.py::create_worktree`

## How to Explore

```
get_communities with id: "community-34"
smart_context with task: "understand . +2 dirs · run", format: "gcx"
find_usages with id: "tests/conftest.py::git_repo", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
