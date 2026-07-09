"""L0 Mailbox — queen-owned agent-addressed message store."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from skep.queen.addressing import resolve_address

log = logging.getLogger(__name__)


class PermanentDeliveryError(Exception):
    """Raised by a deliver_ceo callback when a push can NEVER succeed on retry
    (e.g. the body exceeds the transport's hard length limit). Signals
    redeliver_ceo to dead-letter the message and move on instead of retrying
    it forever and wedging the whole CEO queue behind it."""


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
    def open(cls, path: str) -> Mailbox:
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
            (
                sender,
                recipient,
                subject,
                body,
                created_at,
                in_reply_to,
                hops,
                status,
                dead_letter_reason,
            ),
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

    def _fetch_unread(self, recipient: str) -> list[Message]:
        rows = self._conn.execute(
            "SELECT * FROM messages "
            "WHERE recipient = ? AND status = ? "
            "ORDER BY created_at, id",
            (recipient, STATUS_UNREAD),
        ).fetchall()
        return [_row_to_message(r) for r in rows]

    def _archive(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self._conn.execute(
            f"UPDATE messages SET status = ? WHERE id IN ({placeholders})",
            (STATUS_READ, *ids),
        )
        self._conn.commit()

    def pending(self, recipient: str) -> list[Message]:
        """Unread messages for a recipient, oldest first, WITHOUT archiving.

        Unlike read_inbox (which marks fetched rows read), this is a
        non-destructive peek, used by CEO at-least-once redelivery to retry
        pushes without consuming the messages."""
        return self._fetch_unread(recipient)

    def read_inbox(self, recipient: str) -> list[Message]:
        msgs = self._fetch_unread(recipient)
        self._archive([m.id for m in msgs])
        return msgs

    def count_recent(self, sender: str, since: float) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE sender = ? AND created_at >= ? AND status != ?",
            (sender, since, STATUS_DEAD),
        ).fetchone()
        return int(row["n"])

    def find_duplicate(
        self,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        since: float,
    ) -> Message | None:
        row = self._conn.execute(
            "SELECT * FROM messages "
            "WHERE sender = ? AND recipient = ? AND subject = ? AND body = ? "
            "  AND created_at >= ? AND status != ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (sender, recipient, subject, body, since, STATUS_DEAD),
        ).fetchone()
        return _row_to_message(row) if row else None

    def dead_letter_for(self, message_id: int, reason: str) -> None:
        self._conn.execute(
            "UPDATE messages SET status = ?, dead_letter_reason = ? WHERE id = ?",
            (STATUS_DEAD, reason, message_id),
        )
        self._conn.commit()


class _Entryish(Protocol):
    ref: int
    status: str


class _Bookkeepingish(Protocol):
    def get(self, ref: int) -> _Entryish | None: ...
    def by_worker_task(
        self, host: str, profile: str, local_id: int
    ) -> _Entryish | None: ...


@dataclass
class SendResult:
    ok: bool
    message_id: int | None
    error: str | None
    status: str  # "delivered" | "duplicate" | "rejected" | "dead_letter"


def agent_sender(
    bookkeeping: _Bookkeepingish,
    host: str,
    profile: str,
    local_id: int,
) -> str:
    entry = bookkeeping.by_worker_task(host, profile, local_id)
    if entry is None:
        raise ValueError(f"no bookkeeping entry for {host}/{profile}/{local_id}")
    return str(entry.ref)


class MailboxService:
    def __init__(
        self,
        mailbox: Mailbox,
        bookkeeping: _Bookkeepingish,
        managers: set[str],
        deliver_ceo: Callable[[Message], Awaitable[None]],
        alert_ceo: Callable[[str], Awaitable[None]],
        *,
        rate_limit: int = 20,
        rate_window: float = 60.0,
        depth_cap: int = 10,
        dedupe_window: float = 60.0,
        body_cap: int = 16384,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._mailbox = mailbox
        self._bk = bookkeeping
        self._managers = managers
        self._deliver_ceo = deliver_ceo
        self._alert_ceo = alert_ceo
        self._rate_limit = rate_limit
        self._rate_window = rate_window
        self._depth_cap = depth_cap
        self._dedupe_window = dedupe_window
        self._body_cap = body_cap
        self._now = now
        # Serializes redeliver_ceo so an on-send drain and the periodic sweep
        # never interleave and double-push the same message to the human.
        self._ceo_lock = asyncio.Lock()

    async def handle_send(
        self,
        sender: str,
        to: str,
        subject: str,
        body: str,
        in_reply_to: int | None = None,
    ) -> SendResult:
        now = self._now()

        if len(body.encode("utf-8")) > self._body_cap:
            return SendResult(
                False, None, f"body too large (>{self._body_cap} bytes)", "rejected"
            )

        res = resolve_address(to, self._bk, self._managers)
        if res.kind == "invalid":
            return SendResult(False, None, res.error, "rejected")
        recipient = res.canonical

        recent = self._mailbox.count_recent(sender, since=now - self._rate_window)
        if recent >= self._rate_limit:
            return SendResult(
                False,
                None,
                f"rate limit exceeded ({self._rate_limit}/{self._rate_window:g}s)",
                "rejected",
            )

        dup = self._mailbox.find_duplicate(
            sender, recipient, subject, body, since=now - self._dedupe_window
        )
        if dup is not None:
            return SendResult(True, dup.id, None, "duplicate")

        if in_reply_to is not None:
            parent = self._mailbox.get(in_reply_to)
            if parent is None:
                return SendResult(
                    False, None, f"in_reply_to {in_reply_to} does not exist", "rejected"
                )
            hops = parent.hops + 1
        else:
            hops = 0

        if hops > self._depth_cap:
            mid = self._mailbox.insert(
                sender=sender,
                recipient=recipient,
                subject=subject,
                body=body,
                created_at=now,
                in_reply_to=in_reply_to,
                hops=hops,
                status=STATUS_DEAD,
                dead_letter_reason=f"depth cap exceeded ({hops}>{self._depth_cap})",
            )
            await self._safe_alert(
                f"⚠️ mailbox loop stopped: {sender}→{recipient} "
                f"'{subject}' exceeded depth cap ({hops})"
            )
            return SendResult(False, mid, "depth cap exceeded", "dead_letter")

        mid = self._mailbox.insert(
            sender=sender,
            recipient=recipient,
            subject=subject,
            body=body,
            created_at=now,
            in_reply_to=in_reply_to,
            hops=hops,
        )

        if res.kind == "ic":
            # Recipient-gone guard (defense-in-depth). resolve_address above
            # confirmed the IC recipient was active, but nothing serializes
            # that check against the recipient finishing (on_done ->
            # handle_recipient_gone). Today handle_send runs resolve->insert
            # with no await between them, so the interleave can't fire on the
            # single queen event loop -- but re-checking liveness right after
            # insert closes the window deterministically if an await is ever
            # introduced here, rather than leaving the message unread in a
            # finished agent's inbox forever.
            if resolve_address(to, self._bk, self._managers).kind != "ic":
                self._mailbox.dead_letter_for(
                    mid, f"recipient {recipient} finished before delivery"
                )
                await self._safe_alert(
                    f"⚠️ message to agent {recipient} undeliverable: "
                    f"recipient finished before delivery"
                )
                return SendResult(False, mid, "recipient finished", "dead_letter")

        if res.kind == "ceo":
            # Push is decoupled from acceptance: redeliver_ceo drains all
            # pending CEO mail in order and marks each read only after a
            # successful push, so a Telegram outage leaves the message
            # pending for retry instead of losing it (at-least-once).
            await self.redeliver_ceo()

        return SendResult(True, mid, None, "delivered")

    async def redeliver_ceo(self) -> None:
        """Push all pending (unread) CEO mail in creation order, marking each
        read only after a successful delivery. Stops at the first failure,
        leaving that message -- and any later ones -- unread for the next
        retry, so a transient Telegram outage never silently drops CEO mail
        (at-least-once). Head-of-line: a message that never delivers blocks
        later ones -- acceptable because delivery failures here are transient
        transport errors (content is MarkdownV2-escaped upstream, so
        content-based rejections do not occur). A push that can never succeed
        (PermanentDeliveryError, e.g. body over the transport length limit) is
        dead-lettered and skipped so it cannot wedge the queue behind it."""
        async with self._ceo_lock:
            for msg in self._mailbox.pending("ceo"):
                try:
                    await self._deliver_ceo(msg)
                except PermanentDeliveryError as exc:
                    self._mailbox.dead_letter_for(msg.id, f"undeliverable: {exc}")
                    await self._safe_alert(
                        f"⚠️ CEO message {msg.id} undeliverable: {exc}"
                    )
                    continue
                except Exception:
                    log.warning(
                        "CEO delivery failed for message %s; leaving pending for retry",
                        msg.id,
                        exc_info=True,
                    )
                    return
                self._mailbox.mark_read(msg.id)

    async def _safe_alert(self, text: str) -> None:
        """Best-effort CEO alert: a failed alert push must never crash the
        caller (send pipeline / task-done handler)."""
        try:
            await self._alert_ceo(text)
        except Exception:
            log.warning("CEO alert failed: %s", text, exc_info=True)

    async def handle_read(self, recipient: str) -> list[Message]:
        return self._mailbox.read_inbox(recipient)

    async def handle_recipient_gone(self, ref: int) -> None:
        pending = self._mailbox.read_inbox(str(ref))
        # read_inbox archived them as READ; re-mark as dead-letter.
        for m in pending:
            self._mailbox.dead_letter_for(m.id, f"recipient {ref} finished")
        if pending:
            await self._safe_alert(
                f"⚠️ {len(pending)} message(s) undeliverable: "
                f"agent {ref} finished before reading its inbox"
            )
