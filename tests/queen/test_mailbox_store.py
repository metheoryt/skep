from skep.queen.mailbox import (
    Mailbox,
    Message,
    STATUS_UNREAD,
    STATUS_READ,
)


def _mb() -> Mailbox:
    return Mailbox.open(":memory:")


def test_insert_and_get_roundtrip():
    mb = _mb()
    mid = mb.insert(
        sender="ceo",
        recipient="mgr:alice",
        subject="hello",
        body="world",
        created_at=100.0,
        in_reply_to=None,
        hops=0,
    )
    assert mid == 1
    msg = mb.get(mid)
    assert msg == Message(
        id=1,
        sender="ceo",
        recipient="mgr:alice",
        subject="hello",
        body="world",
        created_at=100.0,
        in_reply_to=None,
        hops=0,
        status=STATUS_UNREAD,
        dead_letter_reason=None,
    )


def test_get_missing_returns_none():
    assert _mb().get(999) is None


def test_mark_read():
    mb = _mb()
    mid = mb.insert(
        sender="ceo", recipient="mgr:a", subject="s", body="b",
        created_at=1.0, in_reply_to=None, hops=0,
    )
    mb.mark_read(mid)
    assert mb.get(mid).status == STATUS_READ


def test_read_inbox_returns_unread_oldest_first_and_archives():
    mb = _mb()
    m1 = mb.insert(sender="ceo", recipient="mgr:a", subject="s1", body="b1",
                   created_at=1.0, in_reply_to=None, hops=0)
    m2 = mb.insert(sender="ceo", recipient="mgr:a", subject="s2", body="b2",
                   created_at=2.0, in_reply_to=None, hops=0)
    # different recipient, must not leak
    mb.insert(sender="ceo", recipient="mgr:b", subject="x", body="y",
              created_at=1.5, in_reply_to=None, hops=0)

    got = mb.read_inbox("mgr:a")
    assert [m.id for m in got] == [m1, m2]
    # now archived
    assert mb.get(m1).status == STATUS_READ
    assert mb.get(m2).status == STATUS_READ
    # second read is empty (pure pull, already archived)
    assert mb.read_inbox("mgr:a") == []


def test_read_inbox_only_archives_fetched_rows():
    from skep.queen.mailbox import Mailbox

    class _RacyMailbox(Mailbox):
        def _fetch_unread(self, recipient):
            rows = super()._fetch_unread(recipient)
            # simulate a concurrent insert landing after the SELECT
            self.insert(sender="x", recipient=recipient, subject="late",
                        body="b", created_at=99.0, in_reply_to=None, hops=0)
            return rows

    mb = _RacyMailbox.open(":memory:")
    mb.insert(sender="ceo", recipient="mgr:a", subject="s1", body="b1",
              created_at=1.0, in_reply_to=None, hops=0)
    got = mb.read_inbox("mgr:a")
    assert [m.subject for m in got] == ["s1"]
    # the message that "arrived" during the read must NOT be archived
    survivors = mb.read_inbox("mgr:a")
    assert [m.subject for m in survivors] == ["late"]
