import os

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.environ.get("SKEP_RUN_INTEGRATION") != "1",
    reason="requires a real `claude` CLI; set SKEP_RUN_INTEGRATION=1",
)
async def test_agent_can_send_via_mcp_shim(tmp_path):
    """Real claude -> --mcp-config -> live MailboxShim -> mailbox store."""
    from skep.queen.mailbox import Mailbox, MailboxService
    from skep.transport import InMemoryMailboxClient
    from skep.worker.mcp_config import write_mcp_config
    from skep.worker.mcp_shim import MailboxShim
    from skep.agent import AgentProcess

    delivered = []

    async def deliver_ceo(msg):
        delivered.append(msg)

    async def alert_ceo(text):
        pass

    class _Bk:
        def get(self, ref): return None
        def by_worker_task(self, h, p, l): return None

    mailbox = Mailbox.open(":memory:")
    service = MailboxService(mailbox, _Bk(), set(), deliver_ceo, alert_ceo)
    client = InMemoryMailboxClient(service, lambda tid: "mgr:alice")
    shim = MailboxShim(client, tid=1)
    url = await shim.start()
    try:
        servers = {"mailbox": {"type": "http", "url": url}}
        cfg_path = write_mcp_config(tmp_path, servers)
        agent = AgentProcess(
            "Use the send_message tool to send to 'ceo' with subject 'hi' "
            "and body 'from agent', then stop.",
            str(tmp_path), "claude",
            mcp_config_path=str(cfg_path))
        await agent.start()
        # Drain stdout events; this awaits subprocess exit internally.
        async for _event in agent.events():
            pass
    finally:
        await shim.stop()

    assert any(m.subject == "hi" for m in delivered)
