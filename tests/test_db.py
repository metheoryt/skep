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
