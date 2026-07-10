"""Stdio MCP server exposing one tool: `remember`.

Unlike `MailboxShim` this is NOT an in-process HTTP server. `remember` is a
stateless local file write parameterized by one repo path -- it needs no
network hop, so it needs no uvicorn, no ephemeral port, and no bearer token on
the agent's command line (spec §5.2). `claude` spawns this as its own child and
reaps it on exit; `Supervisor` contributes a dict entry and nothing else.

The parent repo path arrives as argv[1], so the agent cannot influence which
repo it writes to -- the same closed-over-identity property `tid` gives the
mailbox.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from skep.memory import write_memory

MEMORY_TOOLS: tuple[str, ...] = ("mcp__memory__remember",)
"""Exact --allowedTools names. Never a wildcard (spec §8.1)."""


def memory_shim_server(repo_path: Path) -> dict[str, object]:
    """The `--mcp-config` entry for this repo's memory shim."""
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "skep.worker.memory_shim", str(repo_path)],
    }


def build_remember(repo_path: Path) -> Callable[..., str]:
    """The `remember` implementation, closed over the PARENT repo path."""

    def remember(
        title: str,
        body: str,
        kind: str = "gotcha",
        supersedes: str | None = None,
    ) -> str:
        """Record a durable fact about this repo for future agents.

        Call this when you learn something that would save the next agent
        time and that the repo itself does not already record: an operational
        fact, a constraint you discovered the hard way, or a decision and its
        reasoning. If a fact you were shown is now wrong, pass its bracketed
        slug as `supersedes` rather than adding a contradiction.

        kind: one of gotcha, constraint, decision, convention, incident.
        Returns the path of the written memory file.
        """
        # Raises on a bad title/supersedes; the error reaches the agent as a
        # tool error rather than a silent no-op it believes succeeded.
        return str(write_memory(repo_path, title, body, kind, supersedes))

    return remember


def build_server(repo_path: Path) -> FastMCP:
    server = FastMCP("memory")
    server.tool()(build_remember(repo_path))
    return server


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m skep.worker.memory_shim <repo-path>")
    build_server(Path(sys.argv[1])).run(transport="stdio")


if __name__ == "__main__":
    main()
