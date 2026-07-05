import pytest

from dataclasses import dataclass

from skep.queen.mailbox import (
    Mailbox,
    MailboxService,
    SendResult,
    agent_sender,
    STATUS_DEAD,
)


def _mb() -> Mailbox:
    return Mailbox.open(":memory:")


def _ins(mb, *, sender="mgr:a", recipient="ceo", subject="s", body="b",
         created_at=1.0):
    return mb.insert(sender=sender, recipient=recipient, subject=subject,
                     body=body, created_at=created_at, in_reply_to=None, hops=0)


def test_count_recent_windows_by_time_and_sender():
    mb = _mb()
    _ins(mb, created_at=10.0)
    _ins(mb, created_at=20.0)
    _ins(mb, sender="mgr:other", created_at=20.0)
    assert mb.count_recent("mgr:a", since=15.0) == 1
    assert mb.count_recent("mgr:a", since=5.0) == 2


def test_count_recent_excludes_dead_letters():
    mb = _mb()
    mid = _ins(mb, created_at=10.0)
    mb.dead_letter_for(mid, "depth cap")
    assert mb.count_recent("mgr:a", since=0.0) == 0


def test_find_duplicate_matches_identical_within_window():
    mb = _mb()
    first = _ins(mb, subject="hi", body="there", created_at=10.0)
    dup = mb.find_duplicate("mgr:a", "ceo", "hi", "there", since=5.0)
    assert dup is not None and dup.id == first


def test_find_duplicate_ignores_outside_window_or_different_content():
    mb = _mb()
    _ins(mb, subject="hi", body="there", created_at=10.0)
    assert mb.find_duplicate("mgr:a", "ceo", "hi", "there", since=15.0) is None
    assert mb.find_duplicate("mgr:a", "ceo", "hi", "other", since=5.0) is None


def test_dead_letter_for_sets_status_and_reason():
    mb = _mb()
    mid = _ins(mb)
    mb.dead_letter_for(mid, "loop depth exceeded")
    msg = mb.get(mid)
    assert msg.status == STATUS_DEAD
    assert msg.dead_letter_reason == "loop depth exceeded"


def test_find_duplicate_excludes_dead_letters():
    mb = _mb()
    mid = _ins(mb, subject="hi", body="there", created_at=10.0)
    mb.dead_letter_for(mid, "loop")
    assert mb.find_duplicate("mgr:a", "ceo", "hi", "there", since=5.0) is None


def test_find_duplicate_returns_most_recent():
    mb = _mb()
    _ins(mb, subject="hi", body="there", created_at=10.0)
    newer = _ins(mb, subject="hi", body="there", created_at=20.0)
    dup = mb.find_duplicate("mgr:a", "ceo", "hi", "there", since=5.0)
    assert dup is not None and dup.id == newer


def test_count_recent_boundary_is_inclusive():
    mb = _mb()
    _ins(mb, created_at=10.0)
    assert mb.count_recent("mgr:a", since=10.0) == 1


@dataclass
class _Entry:
    ref: int
    status: str = "running"
    host: str = "h"
    profile: str = "p"
    local_id: int = 1


class _Bk:
    def __init__(self, by_ref=None, by_wt=None):
        self._by_ref = by_ref or {}
        self._by_wt = by_wt or {}

    def get(self, ref):
        return self._by_ref.get(ref)

    def by_worker_task(self, host, profile, local_id):
        return self._by_wt.get((host, profile, local_id))


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _svc(bk=None, managers=None, clock=None, **kw):
    delivered = []
    alerts = []

    async def deliver_ceo(msg):
        delivered.append(msg)

    async def alert_ceo(text):
        alerts.append(text)

    svc = MailboxService(
        Mailbox.open(":memory:"),
        bk or _Bk(),
        managers if managers is not None else {"alice"},
        deliver_ceo,
        alert_ceo,
        now=clock or _Clock(),
        **kw,
    )
    return svc, delivered, alerts


async def test_send_to_ceo_delivers():
    svc, delivered, _ = _svc()
    res = await svc.handle_send(sender="mgr:alice", to="ceo",
                                subject="s", body="b")
    assert res.ok and res.status == "delivered"
    assert len(delivered) == 1 and delivered[0].id == res.message_id


async def test_send_to_manager_persists_not_delivered_to_ceo():
    svc, delivered, _ = _svc()
    res = await svc.handle_send(sender="ceo", to="mgr:alice",
                                subject="s", body="b")
    assert res.ok and res.status == "delivered"
    assert delivered == []
    # recipient can pull it
    inbox = await svc.handle_read("mgr:alice")
    assert [m.id for m in inbox] == [res.message_id]


async def test_invalid_address_rejected():
    svc, _, _ = _svc()
    res = await svc.handle_send(sender="ceo", to="mgr:ghost",
                                subject="s", body="b")
    assert not res.ok and res.status == "rejected"
    assert "ghost" in res.error


async def test_body_cap_rejects():
    svc, _, _ = _svc(body_cap=10)
    res = await svc.handle_send(sender="ceo", to="mgr:alice",
                                subject="s", body="x" * 11)
    assert not res.ok and res.status == "rejected"
    assert "too large" in res.error.lower()


async def test_rate_limit_rejects_over_quota():
    clock = _Clock(1000.0)
    svc, _, _ = _svc(clock=clock, rate_limit=2, rate_window=60.0)
    assert (await svc.handle_send(sender="ceo", to="mgr:alice",
                                  subject="s", body="1")).ok
    assert (await svc.handle_send(sender="ceo", to="mgr:alice",
                                  subject="s", body="2")).ok
    third = await svc.handle_send(sender="ceo", to="mgr:alice",
                                  subject="s", body="3")
    assert not third.ok and third.status == "rejected"
    assert "rate" in third.error.lower()
    # window slides
    clock.t = 1000.0 + 61.0
    assert (await svc.handle_send(sender="ceo", to="mgr:alice",
                                  subject="s", body="4")).ok


async def test_dedupe_is_idempotent():
    svc, delivered, _ = _svc()
    first = await svc.handle_send(sender="ceo", to="mgr:alice",
                                  subject="s", body="same")
    second = await svc.handle_send(sender="ceo", to="mgr:alice",
                                   subject="s", body="same")
    assert second.status == "duplicate"
    assert second.message_id == first.message_id


async def test_depth_cap_dead_letters_and_alerts():
    svc, _, alerts = _svc(depth_cap=2)
    # in_reply_to=parent with hops=2 → child hops=3 > cap
    parent = await svc.handle_send(sender="ceo", to="mgr:alice",
                                   subject="s", body="b")
    # bump parent hops artificially to the cap
    svc._mailbox._conn.execute(
        "UPDATE messages SET hops = 2 WHERE id = ?", (parent.message_id,))
    svc._mailbox._conn.commit()
    res = await svc.handle_send(sender="mgr:alice", to="ceo",
                                subject="re", body="loop",
                                in_reply_to=parent.message_id)
    assert res.status == "dead_letter"
    assert svc._mailbox.get(res.message_id).status == STATUS_DEAD
    assert len(alerts) == 1


async def test_recipient_gone_dead_letters_unread_and_alerts():
    bk = _Bk(by_ref={3: _Entry(ref=3, status="running")})
    svc, _, alerts = _svc(bk=bk)
    r = await svc.handle_send(sender="ceo", to="3", subject="s", body="b")
    assert r.ok
    await svc.handle_recipient_gone(3)
    assert svc._mailbox.get(r.message_id).status == STATUS_DEAD
    assert len(alerts) == 1


def test_agent_sender_resolves_ref():
    bk = _Bk(by_wt={("h", "p", 5): _Entry(ref=42)})
    assert agent_sender(bk, "h", "p", 5) == "42"


def test_agent_sender_unknown_raises():
    with pytest.raises(ValueError):
        agent_sender(_Bk(), "h", "p", 5)
