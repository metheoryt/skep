"""L0 Mailbox — queen-owned agent-addressed message store."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

STATUS_UNREAD = "unread"
STATUS_READ = "read"
STATUS_DEAD = "dead_letter"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    sender             TEXT NOT NULL,
    recipient          TEXT NOT NULL,
    subject            TEXT NOT NULL,
    body               TEXT NOT NULL,
    created_at         REAL NOT NULL,
    in_reply_to        INTEGER,
    hops               INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'unread',
    dead_letter_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_recipient_status
    ON messages (recipient, status, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_sender_created
    ON messages (sender, created_at);
"""


@dataclass
class Message:
    id: int
    sender: str
    recipient: str
    subject: str
    body: str
    created_at: float
    in_reply_to: int | None
    hops: int
    status: str
    dead_letter_reason: str | None


def _row_to_message(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        sender=row["sender"],
        recipient=row["recipient"],
        subject=row["subject"],
        body=row["body"],
        created_at=row["created_at"],
        in_reply_to=row["in_reply_to"],
        hops=row["hops"],
        status=row["status"],
        dead_letter_reason=row["dead_letter_reason"],
    )


class Mailbox:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @classmethod
    def open(cls, path: str) -> "Mailbox":
        return cls(sqlite3.connect(path))

    def insert(
        self,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        created_at: float,
        in_reply_to: int | None,
        hops: int,
        status: str = STATUS_UNREAD,
        dead_letter_reason: str | None = None,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO messages "
            "(sender, recipient, subject, body, created_at, in_reply_to, "
            " hops, status, dead_letter_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sender, recipient, subject, body, created_at, in_reply_to,
             hops, status, dead_letter_reason),
        )
        self._conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def get(self, message_id: int) -> Message | None:
        row = self._conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return _row_to_message(row) if row else None

    def mark_read(self, message_id: int) -> None:
        self._conn.execute(
            "UPDATE messages SET status = ? WHERE id = ?",
            (STATUS_READ, message_id),
        )
        self._conn.commit()

    def read_inbox(self, recipient: str) -> list[Message]:
        rows = self._conn.execute(
            "SELECT * FROM messages "
            "WHERE recipient = ? AND status = ? "
            "ORDER BY created_at, id",
            (recipient, STATUS_UNREAD),
        ).fetchall()
        msgs = [_row_to_message(r) for r in rows]
        if msgs:
            self._conn.execute(
                "UPDATE messages SET status = ? "
                "WHERE recipient = ? AND status = ?",
                (STATUS_READ, recipient, STATUS_UNREAD),
            )
            self._conn.commit()
        return msgs
