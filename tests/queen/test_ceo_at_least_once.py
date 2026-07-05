"""At-least-once CEO delivery (L0.1 hardening).

A failed Telegram push must not silently lose the message: it stays pending
(unread) and is redelivered on the next attempt. Without this, a transient
Telegram failure -> no ack -> sender retries -> dedupe returns the same id
without redelivering -> the push-only CEO never sees the message.
"""

from dataclasses import dataclass

from skep.queen.mailbox import (
    Mailbox,
    MailboxService,
    STATUS_READ,
    STATUS_UNREAD,
)


@dataclass
class _Entry:
    ref: int
    status: str = "running"


class _Bk:
    def get(self, ref):
        return None

    def by_worker_task(self, host, profile, local_id):
        return None


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _svc(deliver):
    alerts = []

    async def alert(text):
        alerts.append(text)

    svc = MailboxService(
        Mailbox.open(":memory:"),
        _Bk(),
        {"alice"},
        deliver,
        alert,
        now=_Clock(),
    )
    return svc, alerts


async def test_ceo_delivery_success_marks_read():
    delivered = []

    async def deliver(msg):
        delivered.append(msg.id)

    svc, _ = _svc(deliver)
    res = await svc.handle_send(sender="mgr:alice", to="ceo",
                                subject="s", body="b")
    assert res.ok
    assert delivered == [res.message_id]
    # a delivered message is archived (READ); nothing remains pending
    assert svc._mailbox.pending("ceo") == []
    assert svc._mailbox.get(res.message_id).status == STATUS_READ


async def test_ceo_delivery_failure_leaves_message_pending():
    calls = []

    async def deliver(msg):
        calls.append(msg.id)
        raise RuntimeError("telegram down")

    svc, _ = _svc(deliver)
    res = await svc.handle_send(sender="mgr:alice", to="ceo",
                                subject="s", body="b")
    # send still reports acceptance into the mailbox (push is decoupled)
    assert res.ok and res.message_id is not None
    # push was attempted but failed -> message stays unread/pending
    assert calls == [res.message_id]
    pending = svc._mailbox.pending("ceo")
    assert [m.id for m in pending] == [res.message_id]
    assert svc._mailbox.get(res.message_id).status == STATUS_UNREAD


async def test_redeliver_ceo_pushes_pending_after_recovery():
    outcomes = iter([RuntimeError("down"), None])  # fail once, then succeed
    calls = []

    async def deliver(msg):
        calls.append(msg.id)
        exc = next(outcomes)
        if exc is not None:
            raise exc

    svc, _ = _svc(deliver)
    res = await svc.handle_send(sender="mgr:alice", to="ceo",
                                subject="s", body="b")
    assert svc._mailbox.get(res.message_id).status == STATUS_UNREAD

    # Telegram recovers; the retry sweep redelivers the pending message.
    await svc.redeliver_ceo()
    assert calls == [res.message_id, res.message_id]  # attempted twice
    assert svc._mailbox.pending("ceo") == []
    assert svc._mailbox.get(res.message_id).status == STATUS_READ


async def test_dedupe_does_not_drop_an_undelivered_ceo_message():
    """The exact silent-loss bug: push fails, sender retries, dedupe returns
    the same id without redelivering. The retry sweep must still deliver it."""
    fail = {"on": True}
    calls = []

    async def deliver(msg):
        calls.append(msg.id)
        if fail["on"]:
            raise RuntimeError("down")

    svc, _ = _svc(deliver)
    first = await svc.handle_send(sender="mgr:alice", to="ceo",
                                  subject="s", body="b")
    # sender retries within the dedupe window -> duplicate, no new insert
    dup = await svc.handle_send(sender="mgr:alice", to="ceo",
                                subject="s", body="b")
    assert dup.status == "duplicate" and dup.message_id == first.message_id
    assert svc._mailbox.get(first.message_id).status == STATUS_UNREAD

    fail["on"] = False
    await svc.redeliver_ceo()
    assert svc._mailbox.get(first.message_id).status == STATUS_READ


async def test_redeliver_stops_at_first_failure_and_preserves_order():
    fail = {"on": True}
    order = []

    async def deliver(msg):
        order.append(msg.id)
        if fail["on"]:
            raise RuntimeError("down")

    svc, _ = _svc(deliver)
    m1 = await svc.handle_send(sender="mgr:alice", to="ceo",
                               subject="s1", body="b1")
    m2 = await svc.handle_send(sender="mgr:alice", to="ceo",
                               subject="s2", body="b2")
    # both undelivered, kept in creation order
    assert [m.id for m in svc._mailbox.pending("ceo")] == [
        m1.message_id, m2.message_id]

    # sweep while still down: attempts the oldest, fails, stops (no m2 attempt)
    order.clear()
    await svc.redeliver_ceo()
    assert order == [m1.message_id]

    # recover: both delivered, in order
    fail["on"] = False
    order.clear()
    await svc.redeliver_ceo()
    assert order == [m1.message_id, m2.message_id]
    assert svc._mailbox.pending("ceo") == []
