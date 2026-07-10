from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

SCHEMA_VERSION = 1

# Baseline (v0) schema. Both fresh and pre-existing DBs are migrated up to
# SCHEMA_VERSION by _migrate(); keeping the baseline here means one code path.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
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
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    at TEXT NOT NULL
);
"""

_ACTIVE = ("spawning", "running")
_TASK_COLUMNS = (
    "id",
    "repo",
    "task",
    "worktree_path",
    "resume_token",
    "model",
    "session_local_id",
    "pid",
    "topic_id",
    "mode",
    "status",
    "created_at",
)


@dataclass
class Task:
    id: int | None
    repo: str
    task: str
    worktree_path: str
    resume_token: str | None
    model: str | None
    session_local_id: int | None
    pid: int | None
    topic_id: int | None
    mode: str
    status: str
    created_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        # v0 -> v1: rename session_id -> resume_token; add model +
        # session_local_id; back-fill session_local_id to the row's own id
        # (each existing task becomes a one-invocation session).
        conn.execute("ALTER TABLE tasks RENAME COLUMN session_id TO resume_token")
        conn.execute("ALTER TABLE tasks ADD COLUMN model TEXT")
        conn.execute("ALTER TABLE tasks ADD COLUMN session_local_id INTEGER")
        conn.execute(
            "UPDATE tasks SET session_local_id = id WHERE session_local_id IS NULL"
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()


class Registry:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    @classmethod
    def open(cls, path: str) -> Registry:
        conn = sqlite3.connect(path)
        conn.executescript(_SCHEMA)
        conn.commit()
        _migrate(conn)
        return cls(conn)

    def add_task(
        self, repo: str, task: str, worktree_path: str, mode: str = "native"
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO tasks (repo, task, worktree_path, mode, status, created_at)"
            " VALUES (?, ?, ?, ?, 'spawning', ?)",
            (repo, task, worktree_path, mode, _now()),
        )
        self._conn.commit()
        assert cur.lastrowid is not None  # guaranteed set after INSERT
        return cur.lastrowid

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        return Task(**{c: row[c] for c in _TASK_COLUMNS})

    def get_task(self, task_id: int) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return self._row_to_task(row) if row else None

    def list_all(self) -> list[Task]:
        rows = self._conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        return [self._row_to_task(r) for r in rows]

    def list_active(self) -> list[Task]:
        placeholders = ",".join("?" for _ in _ACTIVE)
        rows = self._conn.execute(
            f"SELECT * FROM tasks WHERE status IN ({placeholders}) ORDER BY id",
            _ACTIVE,
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def update(self, task_id: int, **fields: object) -> None:
        if not fields:
            return
        allowed = set(_TASK_COLUMNS) - {"id", "created_at"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update columns: {bad}")
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?",
            (*fields.values(), task_id),
        )
        self._conn.commit()

    def log_audit(self, task_id: int | None, kind: str, detail: str) -> None:
        self._conn.execute(
            "INSERT INTO audit (task_id, kind, detail, at) VALUES (?, ?, ?, ?)",
            (task_id, kind, detail, _now()),
        )
        self._conn.commit()

    def audit_rows(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM audit ORDER BY id").fetchall()

    def close(self) -> None:
        self._conn.close()
