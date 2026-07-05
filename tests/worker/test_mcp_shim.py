from skep.worker.mcp_shim import MailboxShim
from skep.transport import SendReply


class _FakeClient:
    def __init__(self):
        self.sent = []
        self.reads = []

    async def send(self, tid, to, subject, body, in_reply_to):
        self.sent.append((tid, to, subject, body, in_reply_to))
        return SendReply(True, 1, None, "delivered")

    async def read(self, tid):
        self.reads.append(tid)
        return [{"id": 1, "sender": "ceo", "subject": "s", "body": "b",
                 "created_at": 1.0, "in_reply_to": None}]


async def test_send_message_tool_binds_tid_and_forwards():
    client = _FakeClient()
    shim = MailboxShim(client, tid=9)
    result = await shim._tools()["send_message"](
        to="ceo", subject="s", body="b")
    # tid comes from the shim closure, NOT tool input
    assert client.sent == [(9, "ceo", "s", "b", None)]
    assert result["ok"] is True
    assert result["status"] == "delivered"


async def test_read_inbox_tool_binds_tid_and_forwards():
    client = _FakeClient()
    shim = MailboxShim(client, tid=9)
    result = await shim._tools()["read_inbox"]()
    assert client.reads == [9]
    assert result["messages"][0]["subject"] == "s"


async def test_send_message_reports_rejection():
    class _Reject:
        async def send(self, *a, **k):
            return SendReply(False, None, "unknown manager 'x'", "rejected")

        async def read(self, tid):
            return []

    shim = MailboxShim(_Reject(), tid=1)
    result = await shim._tools()["send_message"](
        to="mgr:x", subject="s", body="b")
    assert result["ok"] is False
    assert "unknown manager" in result["error"]


def test_build_server_constructs_without_raising():
    """Catches SDK API-name errors at unit-test time (no live server needed)."""
    client = _FakeClient()
    shim = MailboxShim(client, tid=1)
    server = shim._build_server()
    assert server is not None
