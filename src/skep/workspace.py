from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

MODE_NEW = "new"
MODE_ATTACH = "attach"
MODE_PRIMARY = "primary"

ACCESS_RW = "rw"
ACCESS_RO = "ro"


@dataclass(frozen=True)
class Root:
    """One directory a session operates in.

    `mode` is how the session relates to the directory:
    - new: create and own a fresh worktree (today's behavior; the default).
    - attach: join an existing (shared) worktree at `path`; `attach_ref` names it.
    - primary: operate in the repo's main checkout at `path`.

    `access` is orthogonal: rw may write, ro is read-only (advisory in A1 --
    real enforcement is Phase 4). A1 never resolves names: `path` is already
    a concrete local path (C's job, upstream).
    """

    name: str
    path: Path
    mode: str = MODE_NEW
    access: str = ACCESS_RW
    attach_ref: str | None = None


@dataclass(frozen=True)
class Workspace:
    roots: list[Root] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.roots:
            raise ValueError("a workspace needs at least one root")

    @classmethod
    def single(cls, name: str, path: Path) -> Workspace:
        """Today's default: one own worktree, read-write."""
        return cls(roots=[Root(name, path, mode=MODE_NEW, access=ACCESS_RW)])

    @property
    def primary_path(self) -> Path:
        return self.roots[0].path

    @property
    def add_dir_paths(self) -> list[Path]:
        return [r.path for r in self.roots[1:]]

    @property
    def requires_lease(self) -> bool:
        # An exclusive queen-held lease is needed exactly when a non-owned root
        # is opened rw -- i.e. primary:rw (spec §6). Enforcement is A2; A1 only
        # reports the requirement.
        return any(
            r.mode == MODE_PRIMARY and r.access == ACCESS_RW for r in self.roots
        )
