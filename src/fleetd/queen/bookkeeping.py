from __future__ import annotations

import sqlite3
from dataclasses import dataclass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
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

_ACTIVE = ("spawning", "running")
_COLUMNS = ("ref", "host", "profile", "local_id", "repo", "title",
            "topic_id", "activity_msg_id", "status")


@dataclass
class Entry:
    ref: int
    host: str
    profile: str
    local_id: int
    repo: str
    title: str
    topic_id: int
    activity_msg_id: int | None
    status: str


class Bookkeeping:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    @classmethod
    def open(cls, path: str) -> "Bookkeeping":
        conn = sqlite3.connect(path)
        conn.executescript(_SCHEMA)
        conn.commit()
        return cls(conn)

    def _row(self, row: sqlite3.Row) -> Entry:
        return Entry(**{c: row[c] for c in _COLUMNS})

    def add(self, host: str, profile: str, local_id: int, repo: str,
            title: str, topic_id: int) -> int:
        cur = self._conn.execute(
            "INSERT INTO entries (host, profile, local_id, repo, title, topic_id,"
            " status) VALUES (?, ?, ?, ?, ?, ?, 'running')",
            (host, profile, local_id, repo, title, topic_id),
        )
        self._conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def by_worker_task(self, host: str, profile: str, local_id: int) -> Entry | None:
        row = self._conn.execute(
            "SELECT * FROM entries WHERE host=? AND profile=? AND local_id=?"
            " ORDER BY ref DESC LIMIT 1",
            (host, profile, local_id),
        ).fetchone()
        return self._row(row) if row else None

    def get(self, ref: int) -> Entry | None:
        row = self._conn.execute(
            "SELECT * FROM entries WHERE ref=?", (ref,)
        ).fetchone()
        return self._row(row) if row else None

    def set_activity_msg(self, ref: int, msg_id: int) -> None:
        self._conn.execute(
            "UPDATE entries SET activity_msg_id=? WHERE ref=?", (msg_id, ref)
        )
        self._conn.commit()

    def set_status(self, ref: int, status: str) -> None:
        self._conn.execute(
            "UPDATE entries SET status=? WHERE ref=?", (status, ref)
        )
        self._conn.commit()

    def list_active(self) -> list[Entry]:
        placeholders = ",".join("?" for _ in _ACTIVE)
        rows = self._conn.execute(
            f"SELECT * FROM entries WHERE status IN ({placeholders}) ORDER BY ref",
            _ACTIVE,
        ).fetchall()
        return [self._row(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
