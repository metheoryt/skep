"""Write a spawned agent's MCP-servers map to a 0600 file in its worktree.

Keeps the per-agent mailbox bearer token (inside the map's mailbox entry) OFF
the agent's argv -- out of /proc/<pid>/cmdline and `ps`, which are
world-readable, so this closes the different-UID / other-local-user vector. A
same-UID sibling can still read this 0600 file (same owner) and the worker's
/proc/<worker-pid>/environ; that closes only with Increment 2's mount/PID
namespaces. Increment 1's win here is purely removing the token from the
command line.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _resolve_exclude_path(worktree: Path) -> Path | None:
    """This worktree's git info/exclude, or None if not a repo.

    For a linked worktree, info/exclude lives in the shared common dir;
    `git rev-parse --git-path` returns the correct path (relative to -C).
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--git-path",
             "info/exclude"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    p = Path(out)
    return p if p.is_absolute() else worktree / p


def _exclude_skep_dir(worktree: Path) -> None:
    """Best-effort: append `/.skep/` to the worktree's git exclude (idempotent).

    Stops an agent's `git add -A` from committing its own token file. A git
    hiccup must NEVER abort a spawn -- the 0600 file is the real guard.
    """
    exclude = _resolve_exclude_path(worktree)
    if exclude is None:
        return
    try:
        existing = exclude.read_text() if exclude.exists() else ""
        if "/.skep/" in existing.splitlines():
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write("/.skep/\n")
    except OSError:
        return


def write_mcp_config(worktree: Path, mcp_servers: dict[str, dict]) -> Path:
    """Write `{"mcpServers": mcp_servers}` to a 0600 file in the agent's
    worktree and return its path. The token (in the mailbox entry) rides this
    file, not argv."""
    skep_dir = worktree / ".skep"
    skep_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(skep_dir, 0o700)
    path = skep_dir / "mcp.json"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        # BEFORE writing: O_TRUNC keeps a pre-existing file's looser mode, so a
        # stale mcp.json would otherwise expose the token during the write.
        os.fchmod(fd, 0o600)
        json.dump({"mcpServers": mcp_servers}, fh)
    os.chmod(path, 0o600)  # exact final mode regardless of umask
    _exclude_skep_dir(worktree)
    return path
