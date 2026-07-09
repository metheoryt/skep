"""Throwaway stdio MCP server for the L1.1 permission probe.

One tool, `ping`. It writes a marker file into the directory passed as argv[1],
so that "the agent called the tool" is established by filesystem evidence rather
than by the agent's self-report.
"""

import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

MARKER_DIR = Path(sys.argv[1]).resolve()

mcp = FastMCP("stub")


@mcp.tool()
def ping() -> str:
    """Record a probe marker. Call this when asked to ping."""
    (MARKER_DIR / "ping-marker").write_text("ping tool executed\n")
    return "pong"


if __name__ == "__main__":
    mcp.run(transport="stdio")
