"""Spawns a REAL `claude` with both MCP servers and fires a tool from each.

Gated behind SKEP_RUN_INTEGRATION=1 -- it costs tokens and needs a logged-in
`claude` on PATH. The mailbox's own e2e test is skipped for the same reason,
which is precisely why nobody noticed the mailbox was unreachable (spec §2.1).
"""

import os
import shutil

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SKEP_RUN_INTEGRATION") != "1" or not shutil.which("claude"),
    reason="needs SKEP_RUN_INTEGRATION=1 and a logged-in `claude` on PATH",
)


async def test_stdio_memory_and_http_mailbox_both_resolve(tmp_path):
    """A stdio entry and an http entry in one --mcp-config, both callable.

    Evidence is out-of-band on the memory side: the file must exist on disk.
    The agent's self-report is not the evidence.
    """
    from skep.agent import AgentProcess
    from skep.supervisor import BASE_TOOLS, MAILBOX_TOOLS
    from skep.worker.mcp_shim import MailboxShim
    from skep.worker.memory_shim import MEMORY_TOOLS, memory_shim_server

    repo = tmp_path / "repo"
    (repo / ".agent-memory").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()

    class CountingMailboxClient:
        """MailboxShim calls `.read(tid)` (mcp_shim.py:113). Count the calls."""

        def __init__(self):
            self.read_calls = 0

        async def read(self, tid):
            self.read_calls += 1
            return []

        async def send(self, *a, **k):  # pragma: no cover - not exercised here
            raise AssertionError("send_message not part of this test")

    # Stand up a real MailboxShim on an ephemeral port, as Supervisor does.
    client = CountingMailboxClient()
    token = "probe-token"
    shim = MailboxShim(client, tid=1, token=token)
    url = await shim.start()
    try:
        agent = AgentProcess(
            task_text=(
                "Do both, then reply DONE. "
                "1. Call the remember tool with title 'Two servers work' and "
                "body 'stdio and http coexist'. "
                "2. Call the read_inbox tool."
            ),
            cwd=wt,
            claude_bin="claude",
            mcp_servers={
                "memory": memory_shim_server([("repo", repo)]),
                "mailbox": {
                    "type": "http",
                    "url": url,
                    "headers": {"Authorization": f"Bearer {token}"},
                },
            },
            allowed_tools=[*BASE_TOOLS, *MEMORY_TOOLS, *MAILBOX_TOOLS],
        )
        await agent.start()
        async for _ in agent.events():
            pass
    finally:
        await shim.stop()

    # Out-of-band evidence that the STDIO server resolved and its tool fired.
    written = list((repo / ".agent-memory").glob("*.md"))
    assert written, "remember never wrote a file: the stdio server did not resolve"
    assert "stdio and http coexist" in written[0].read_text()

    # Out-of-band evidence that the HTTP server resolved and its tool fired,
    # AND that `mcp__mailbox__read_inbox` is the correct grant name despite
    # MailboxShim advertising itself as FastMCP("skep-mailbox").
    assert client.read_calls >= 1, (
        "read_inbox never reached the shim. If `remember` worked but this did "
        "not, the grant name follows the ADVERTISED server name, not the "
        "config map key -- see this task's preamble."
    )
