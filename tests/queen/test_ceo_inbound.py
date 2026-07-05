from skep.app import handle_ceo_reply


class _Svc:
    def __init__(self):
        self.calls = []

    async def handle_send(self, sender, to, subject, body, in_reply_to=None):
        self.calls.append((sender, to, subject, body, in_reply_to))
        from skep.queen.mailbox import SendResult
        return SendResult(True, 1, None, "delivered")


async def test_ceo_reply_sends_as_ceo():
    svc = _Svc()
    res = await handle_ceo_reply(svc, in_reply_to=5, to="mgr:alice",
                                 subject="re", body="ack")
    assert res.ok
    assert svc.calls == [("ceo", "mgr:alice", "re", "ack", 5)]
