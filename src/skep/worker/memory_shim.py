"""Stdio MCP server exposing one tool: `remember`.

Unlike `MailboxShim` this is NOT an in-process HTTP server. `remember` is a
stateless local file write parameterized by one or more named repo roots --
it needs no network hop, so it needs no uvicorn, no ephemeral port, and no
bearer token on the agent's command line (spec §5.2). `claude` spawns this as
its own child and reaps it on exit; `Supervisor` contributes a dict entry and
nothing else.

The workspace roots arrive as `name=path` argv pairs, so the agent cannot
influence which repos exist or where they point -- the same closed-over-
identity property `tid` gives the mailbox. `project` only *selects* among
those closed-over roots.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from skep.memory import write_memory

MEMORY_TOOLS: tuple[str, ...] = ("mcp__memory__remember",)
"""Exact --allowedTools names. Never a wildcard (spec §8.1)."""


def memory_shim_server(roots: list[tuple[str, Path]]) -> dict[str, object]:
    """The `--mcp-config` entry; each root passed as name=path (name has no '=')."""
    args = ["-m", "skep.worker.memory_shim"]
    args += [f"{name}={path}" for name, path in roots]
    return {"type": "stdio", "command": sys.executable, "args": args}


def build_remember(root_paths: dict[str, Path]) -> Callable[..., str]:
    """The `remember` implementation, closed over the workspace's root paths."""

    def remember(
        title: str,
        body: str,
        kind: str = "gotcha",
        supersedes: str | None = None,
        project: str | None = None,
    ) -> str:
        """Record a durable fact about a project in this workspace for future agents.

        Call this when you learn something that would save the next agent
        time and that the repo itself does not already record: an operational
        fact, a constraint you discovered the hard way, or a decision and its
        reasoning. If a fact you were shown is now wrong, pass its bracketed
        slug as `supersedes` rather than adding a contradiction.

        project: which workspace root to write to (by name); defaults to the
        primary root. kind: one of gotcha, constraint, decision, convention,
        incident. Returns the path of the written memory file.
        """
        # Raises on a bad title/supersedes/project; the error reaches the
        # agent as a tool error rather than a silent no-op it believes
        # succeeded.
        return str(write_memory(root_paths, project, title, body, kind, supersedes))

    return remember


def build_server(root_paths: dict[str, Path]) -> FastMCP:
    server = FastMCP("memory")
    server.tool()(build_remember(root_paths))
    return server


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m skep.worker.memory_shim <name>=<path> ...")
    roots: dict[str, Path] = {}
    for arg in sys.argv[1:]:
        name, _, path = arg.partition("=")
        roots[name] = Path(path)
    build_server(roots).run(transport="stdio")


if __name__ == "__main__":
    main()
