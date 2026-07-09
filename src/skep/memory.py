from __future__ import annotations

import asyncio
import logging
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 10.0


def recall_command(repo_path: Path) -> list[str]:
    """The exact invocation the addendum recommends and the preflight probes."""
    return ["gortex", "memory", "recall", "--index", str(repo_path), "--limit", "10"]


def memory_addendum(repo_path: Path) -> str:
    """System-prompt addendum telling an agent it has durable repo memory.

    `repo_path` is the PARENT repo (repos_root/<repo>), never the agent's
    worktree: the gortex daemon tracks the parent, and `--index <parent>` is
    the only form that works from inside a worktree.

    Phrased prescriptively (when to write, not just how) -- prescriptive
    trigger conditions measurably lift correct tool use.
    """
    repo = str(repo_path)
    recall = shlex.join(recall_command(repo_path))
    return f"""## Memory

You have durable memory for this repo, shared with every agent that works on it.

Before starting, recall what is already known:
    {recall}

Store a memory when you learn something that would save the next agent time and
that the repo itself does not already record -- an operational fact (this stack
takes 90s to come up; this test flakes under load), a constraint you discovered
the hard way, or a decision and its reasoning:
    gortex memory store --index {repo} --kind gotcha \\
        --title "<short caption>" --body "<what + why>"

If a memory you find is now wrong, supersede it rather than adding a contradiction:
    gortex memory store --index {repo} --supersedes <id> --body "<corrected>"

Do NOT store what the repo already records -- code structure, git history, CLAUDE.md.
Where the repo holds the fact, reference it instead of copying it.

Scratch notes for this task alone: write a file in your worktree.
"""


async def probe_memory(repo_path: Path, timeout: float = PROBE_TIMEOUT) -> str | None:
    """Smoke-check the exact command the addendum recommends.

    Returns None when memory is available, else a short reason. Never raises:
    the memory dependency is soft and must not be able to fail a spawn. A
    wedged daemon is a real failure mode, hence the timeout.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *recall_command(repo_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return "gortex not found on PATH"
    except OSError as exc:
        return f"could not run gortex: {exc}"

    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return f"gortex did not respond within {timeout:g}s"

    if proc.returncode != 0:
        lines = stderr.decode(errors="replace").strip().splitlines()
        return lines[-1].strip() if lines else f"exit {proc.returncode}"
    return None


Probe = Callable[[Path], Awaitable[str | None]]


class MemoryProbe(Protocol):
    """What Supervisor needs from memory: an addendum, or None."""

    async def addendum_for(self, repo_path: Path) -> str | None: ...


class MemoryPreflight:
    """Probes each repo once, caches the verdict, warns once on unavailability.

    Spec says "preflight once at worker startup"; tracked-ness is per-repo and
    a worker learns its repos dynamically, so once-per-repo-cached is the
    faithful reading. No per-spawn subprocess, no repeated warnings.
    """

    def __init__(self, probe: Probe = probe_memory) -> None:
        self._probe = probe
        self._reasons: dict[Path, str | None] = {}
        self._lock = asyncio.Lock()

    async def addendum_for(self, repo_path: Path) -> str | None:
        async with self._lock:
            if repo_path not in self._reasons:
                reason = await self._probe(repo_path)
                self._reasons[repo_path] = reason
                if reason is not None:
                    logger.warning(
                        "agent memory disabled for %s: %s", repo_path, reason
                    )
        if self._reasons[repo_path] is not None:
            return None
        return memory_addendum(repo_path)
