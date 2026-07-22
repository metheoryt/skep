from __future__ import annotations

from pathlib import Path
from typing import Any

from skep.workspace import (
    ACCESS_RO,
    ACCESS_RW,
    MODE_ATTACH,
    MODE_NEW,
    MODE_PRIMARY,
    Root,
    Workspace,
)


class RootError(ValueError):
    """A root spec the worker refuses to resolve.

    Every refusal is explicit: a silent downgrade (dropping a root, or opening
    it read-only when rw was asked for) would let the queen believe a session
    has access it does not have.
    """


# `attach` is deliberately absent: there is no shared-worktree registry yet,
# so nothing can validate an attach_ref.
_MODES = (MODE_NEW, MODE_PRIMARY)
_ACCESS = (ACCESS_RW, ACCESS_RO)


def _resolve_name(repos_root: Path, name: object) -> Path:
    """Map a repo NAME to a path under repos_root, refusing anything else.

    Names cross the wire; paths never do. `--add-dir` is an arbitrary-read
    primitive, so a name that can escape repos_root would hand a rogue queen
    the contents of the worker's disk (spec section 4).
    """
    if not isinstance(name, str) or not name:
        raise RootError(f"root name must be a non-empty string, got {name!r}")
    if "/" in name or "\\" in name or name == ".." or name.startswith("."):
        raise RootError(f"root name may not contain a path: {name!r}")
    try:
        resolved = (repos_root / name).resolve()
        root_resolved = repos_root.resolve()
    except (ValueError, OSError) as exc:
        raise RootError(f"root name is not a usable path: {name!r}") from exc
    if root_resolved not in resolved.parents:
        raise RootError(f"root {name!r} escapes {repos_root}")
    return repos_root / name


def resolve_roots(repos_root: Path, specs: list[dict[str, Any]]) -> Workspace:
    if not specs:
        raise RootError("a workspace needs at least one root")

    roots: list[Root] = []
    for i, spec in enumerate(specs):
        if not isinstance(spec, dict):
            raise RootError(f"root spec must be an object, got {spec!r}")
        name = spec.get("name")
        mode = spec.get("mode", MODE_NEW)
        access = spec.get("access", ACCESS_RW)

        if mode == MODE_ATTACH:
            raise RootError("attach roots are not supported yet")
        if mode not in _MODES:
            raise RootError(f"unknown root mode: {mode!r}")
        if access not in _ACCESS:
            raise RootError(f"unknown root access: {access!r}")
        if mode == MODE_PRIMARY and access == ACCESS_RW:
            raise RootError(
                "primary:rw needs a queen-held lease, which is not built yet"
            )
        if i == 0 and mode != MODE_NEW:
            # The head root becomes the agent's cwd and holds .skep/mcp.json,
            # whose filename is not tid-keyed yet -- a persistent head root
            # would let concurrent agents clobber each other's token file.
            raise RootError("the head root must be mode 'new'")

        roots.append(
            Root(str(name), _resolve_name(repos_root, name), mode=mode, access=access)
        )

    return Workspace(roots=roots)
