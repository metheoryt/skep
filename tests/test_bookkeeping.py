import sqlite3

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


def test_add_defaults_session_local_id_to_local_id():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("g16", "work", 5, "nix", "t", topic_id=1)
    assert bk.get(ref).session_local_id == 5


def test_add_stores_explicit_session_local_id():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("g16", "work", 9, "nix", "t", topic_id=1, session_local_id=5)
    assert bk.get(ref).session_local_id == 5


def test_by_session_finds_the_row():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("g16", "work", 5, "nix", "t", topic_id=1)
    assert bk.by_session("g16", "work", 5).ref == ref
    assert bk.by_session("g16", "personal", 5) is None
    assert bk.by_session("g16", "work", 99) is None


def test_rebind_invocation_repoints_local_id_and_reactivates():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("g16", "work", 5, "nix", "t", topic_id=1)
    bk.set_status(ref, "done")

    bk.rebind_invocation(ref, 9)

    e = bk.get(ref)
    assert e.local_id == 9
    assert e.status == "running"
    assert e.session_local_id == 5      # the session id never moves
    assert e.topic_id == 1              # the topic never moves
    assert bk.by_worker_task("g16", "work", 9).ref == ref
    assert bk.by_worker_task("g16", "work", 5) is None


def test_migration_backfills_existing_rows(tmp_path):
    # A v0 database written by the shipped code, then opened by this version.
    path = str(tmp_path / "bk.sqlite")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE entries (
            ref INTEGER PRIMARY KEY AUTOINCREMENT,
            host TEXT NOT NULL,
            profile TEXT NOT NULL,
            local_id INTEGER NOT NULL,
            repo TEXT NOT NULL,
            title TEXT NOT NULL,
            topic_id INTEGER NOT NULL,
            activity_msg_id INTEGER,
            status TEXT NOT NULL DEFAULT 'running'
        );
        """
    )
    conn.execute(
        "INSERT INTO entries (host, profile, local_id, repo, title, topic_id)"
        " VALUES ('g16', 'work', 7, 'nix', 'old task', 42)"
    )
    conn.commit()
    conn.close()

    bk = Bookkeeping.open(path)
    e = bk.by_worker_task("g16", "work", 7)
    assert e.session_local_id == 7      # one-invocation session
    assert e.topic_id == 42
    bk.close()

    # Re-opening an already-migrated DB must be a no-op, not an error.
    bk2 = Bookkeeping.open(path)
    assert bk2.by_worker_task("g16", "work", 7).session_local_id == 7
    bk2.close()


def test_park_sets_status_and_until():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("h", "p", 1, "repo", "task", topic_id=10)
    bk.park(ref, until=1000.0)
    e = bk.get(ref)
    assert e.status == "parked"
    assert e.parked_until == 1000.0


def test_parked_due_returns_only_ripe_rows():
    bk = Bookkeeping.open(":memory:")
    a = bk.add("h", "p", 1, "r", "t", topic_id=1)
    b = bk.add("h", "p", 2, "r", "t", topic_id=2)
    bk.park(a, until=100.0)
    bk.park(b, until=300.0)
    due = bk.parked_due(now=200.0)
    assert [e.ref for e in due] == [a]


def test_rebind_invocation_clears_parked_until():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("h", "p", 1, "r", "t", topic_id=1)
    bk.park(ref, until=100.0)
    bk.rebind_invocation(ref, local_id=2)
    e = bk.get(ref)
    assert e.status == "running"
    assert e.parked_until is None
    assert e.local_id == 2


def test_list_active_includes_parked():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("h", "p", 1, "r", "t", topic_id=1)
    bk.park(ref, until=100.0)
    assert [e.ref for e in bk.list_active()] == [ref]
