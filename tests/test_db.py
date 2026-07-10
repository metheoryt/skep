import sqlite3

from skep.db import Registry, Task


def test_add_and_get_task():
    reg = Registry.open(":memory:")
    tid = reg.add_task("nix", "clean nvidia", "/wt/nix-1")
    task = reg.get_task(tid)
    assert task.id == tid
    assert task.repo == "nix"
    assert task.task == "clean nvidia"
    assert task.worktree_path == "/wt/nix-1"
    assert task.mode == "native"
    assert task.status == "spawning"
    assert task.created_at  # non-empty ISO timestamp


def test_update_fields():
    reg = Registry.open(":memory:")
    tid = reg.add_task("nix", "t", "/wt/1")
    reg.update(tid, status="running", pid=999, resume_token="sess-1", topic_id=7)
    task = reg.get_task(tid)
    assert (task.status, task.pid, task.resume_token, task.topic_id) == (
        "running", 999, "sess-1", 7,
    )


def test_list_active_excludes_terminal():
    reg = Registry.open(":memory:")
    a = reg.add_task("r", "a", "/wt/a")
    b = reg.add_task("r", "b", "/wt/b")
    reg.update(a, status="running")
    reg.update(b, status="done")
    active = reg.list_active()
    assert [t.id for t in active] == [a]


def test_get_missing_returns_none():
    reg = Registry.open(":memory:")
    assert reg.get_task(123) is None


def test_audit_log_persists_rows():
    reg = Registry.open(":memory:")
    tid = reg.add_task("r", "t", "/wt/1")
    reg.log_audit(tid, "spawn", "claude -p ...")
    reg.log_audit(None, "panic", "fleet-wide kill")
    rows = reg.audit_rows()
    assert [(r["kind"], r["detail"]) for r in rows] == [
        ("spawn", "claude -p ..."),
        ("panic", "fleet-wide kill"),
    ]


def test_open_migrates_old_schema_file(tmp_path):
    # A pre-migration DB: session_id column, no model/session_local_id, user_version 0.
    db_file = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo TEXT NOT NULL,
            task TEXT NOT NULL,
            worktree_path TEXT NOT NULL,
            session_id TEXT,
            pid INTEGER,
            topic_id INTEGER,
            mode TEXT NOT NULL DEFAULT 'native',
            status TEXT NOT NULL DEFAULT 'spawning',
            created_at TEXT NOT NULL
        );
        CREATE TABLE audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER, kind TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO tasks (repo, task, worktree_path, session_id, status, created_at)"
        " VALUES ('nix', 't', '/wt/nix-1', 'sess-old', 'done', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    reg = Registry.open(str(db_file))
    task = reg.get_task(1)

    assert task.resume_token == "sess-old"          # renamed from session_id
    assert task.model is None                         # new column, back-filled NULL
    assert task.session_local_id == 1                 # back-filled to own id
    assert reg._conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_open_idempotent_on_already_migrated_db(tmp_path):
    # A pre-migration DB: seed it, migrate once, then re-open the same file.
    # The idempotency guard (if version < 1:) must keep the second open a no-op.
    db_file = tmp_path / "already_migrated.sqlite"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo TEXT NOT NULL,
            task TEXT NOT NULL,
            worktree_path TEXT NOT NULL,
            session_id TEXT,
            pid INTEGER,
            topic_id INTEGER,
            mode TEXT NOT NULL DEFAULT 'native',
            status TEXT NOT NULL DEFAULT 'spawning',
            created_at TEXT NOT NULL
        );
        CREATE TABLE audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER, kind TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO tasks (repo, task, worktree_path, session_id, status, created_at)"
        " VALUES ('nix', 't', '/wt/nix-1', 'sess-token-1', 'done', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    # First open: migrate from v0 to v1
    reg1 = Registry.open(str(db_file))
    task1 = reg1.get_task(1)
    assert task1.resume_token == "sess-token-1"
    version1 = reg1._conn.execute("PRAGMA user_version").fetchone()[0]
    assert version1 == 1
    reg1.close()

    # Second open: should NOT re-run the migration (the guard must hold)
    reg2 = Registry.open(str(db_file))
    task2 = reg2.get_task(1)
    # If the idempotency guard broke and the ALTERs re-ran, this would crash
    # (RENAME COLUMN session_id would fail because the column no longer exists).
    assert task2.resume_token == "sess-token-1"
    version2 = reg2._conn.execute("PRAGMA user_version").fetchone()[0]
    assert version2 == 1
    reg2.close()


def test_invocation_queries_group_by_session():
    reg = Registry.open(":memory:")
    # First invocation of a session: session_local_id == own id.
    a = reg.add_task("nix", "t", "/wt/nix-1")
    reg.update(a, session_local_id=a, resume_token="tok-a")
    # A second invocation (resume) of the SAME session, same worktree.
    b = reg.add_task("nix", "t", "/wt/nix-1")
    reg.update(b, session_local_id=a, resume_token="tok-b")
    # An unrelated session.
    c = reg.add_task("web", "u", "/wt/web-3")
    reg.update(c, session_local_id=c)

    invs = reg.list_invocations(a)
    assert [t.id for t in invs] == [a, b]
    assert reg.latest_invocation(a).id == b
    assert reg.latest_invocation(a).resume_token == "tok-b"
    assert reg.latest_invocation(c).id == c
    assert reg.latest_invocation(999) is None
