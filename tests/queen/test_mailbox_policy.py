from skep.queen.mailbox import Mailbox, STATUS_DEAD


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
