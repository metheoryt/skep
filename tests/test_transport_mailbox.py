import pytest

from skep.transport import (
    InMemoryMailboxClient,
    SwitchableMailboxClient,
    MailboxUnavailable,
    SendReply,
)
from skep.queen.mailbox import Mailbox, MailboxService


class _Clock:
    t = 1000.0

    def __call__(self):
        return self.t


class _Bk:
    def get(self, ref):
        return None

    def by_worker_task(self, host, profile, local_id):
        return None


async def _service():
    async def deliver_ceo(msg):
        pass

    async def alert_ceo(text):
        pass

    return MailboxService(
        Mailbox.open(":memory:"), _Bk(), {"alice"},
        deliver_ceo, alert_ceo, now=_Clock())


async def test_in_memory_send_and_read():
    svc = await _service()
    # tid 7 → sender "mgr:alice"
    client = InMemoryMailboxClient(svc, lambda tid: "mgr:alice")
    reply = await client.send(tid=7, to="ceo", subject="s", body="b",
                              in_reply_to=None)
    assert isinstance(reply, SendReply)
    assert reply.ok and reply.status == "delivered"

    # send to a manager, then read from that manager's tid
    await client.send(tid=7, to="mgr:alice", subject="s2", body="b2",
                      in_reply_to=None)
    client2 = InMemoryMailboxClient(svc, lambda tid: "mgr:alice")
    inbox = await client2.read(tid=7)
    assert [m["subject"] for m in inbox] == ["s2"]
    assert inbox[0]["sender"] == "mgr:alice"


async def test_switchable_raises_when_no_target():
    sw = SwitchableMailboxClient()
    with pytest.raises(MailboxUnavailable):
        await sw.send(tid=1, to="ceo", subject="s", body="b", in_reply_to=None)
    with pytest.raises(MailboxUnavailable):
        await sw.read(tid=1)


async def test_switchable_forwards_to_target():
    svc = await _service()
    target = InMemoryMailboxClient(svc, lambda tid: "mgr:alice")
    sw = SwitchableMailboxClient()
    sw.set_target(target)
    reply = await sw.send(tid=7, to="ceo", subject="s", body="b",
                          in_reply_to=None)
    assert reply.ok
