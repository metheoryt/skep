from skep.queen.bookkeeping import Bookkeeping


def test_add_and_lookup_by_worker_task():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("g16", "work", 5, "nix", "clean nvidia", topic_id=555)
    e = bk.by_worker_task("g16", "work", 5)
    assert e.ref == ref
    assert (e.host, e.profile, e.local_id, e.repo, e.topic_id) == (
        "g16", "work", 5, "nix", 555,
    )
    assert e.status == "running"
    assert e.activity_msg_id is None


def test_get_by_ref():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("g16", "work", 5, "nix", "t", topic_id=1)
    assert bk.get(ref).ref == ref
    assert bk.get(999) is None


def test_set_activity_msg_and_status():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("g16", "work", 5, "nix", "t", topic_id=1)
    bk.set_activity_msg(ref, 42)
    bk.set_status(ref, "done")
    e = bk.get(ref)
    assert e.activity_msg_id == 42
    assert e.status == "done"


def test_list_active_excludes_terminal():
    bk = Bookkeeping.open(":memory:")
    a = bk.add("g16", "work", 1, "r", "a", topic_id=1)
    b = bk.add("g16", "work", 2, "r", "b", topic_id=2)
    bk.set_status(b, "done")
    assert [e.ref for e in bk.list_active()] == [a]


def test_worker_task_pairs_are_distinct_per_host_profile():
    bk = Bookkeeping.open(":memory:")
    r1 = bk.add("g16", "work", 5, "r", "a", topic_id=1)
    r2 = bk.add("g16", "personal", 5, "r", "b", topic_id=2)
    assert r1 != r2
    assert bk.by_worker_task("g16", "personal", 5).ref == r2
