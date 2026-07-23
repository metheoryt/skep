from __future__ import annotations

import random
import time
from collections.abc import Callable
from datetime import datetime

from skep.formatting import escape_md
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.mailbox import MailboxService
from skep.telegram_gw import Gateway


class QueenSink:
    """Implements QueenInbox: renders worker domain events into Telegram topics."""

    def __init__(
        self,
        gateway: Gateway,
        bookkeeping: Bookkeeping,
        mailbox_service: MailboxService | None = None,
        *,
        park_default_backoff: float = 3600.0,
        now: Callable[[], float] = time.time,
        jitter: Callable[[], float] = lambda: random.uniform(0.0, 60.0),
    ) -> None:
        self._gw = gateway
        self._bk = bookkeeping
        self._mailbox_service = mailbox_service
        self._park_default_backoff = park_default_backoff
        self._now = now
        self._jitter = jitter

    async def on_task_started(
        self,
        host: str,
        profile: str,
        local_id: int,
        repo: str,
        title: str,
        session_local_id: int | None = None,
    ) -> None:
        if self._bk.by_worker_task(host, profile, local_id) is not None:
            return  # re-attach: worker re-registered an already-known invocation
        if session_local_id is not None:
            prior = self._bk.by_session(host, profile, session_local_id)
            if prior is not None:
                # A later invocation of a known session: the topic follows the
                # session, so reuse it -- and never create a second one.
                self._bk.rebind_invocation(prior.ref, local_id)
                return
        topic_id = await self._gw.create_topic(f"{host}·{profile}·{repo}")
        self._bk.add(
            host,
            profile,
            local_id,
            repo,
            title,
            topic_id,
            session_local_id=session_local_id,
        )

    async def on_activity(
        self, host: str, profile: str, local_id: int, line: str
    ) -> None:
        entry = self._bk.by_worker_task(host, profile, local_id)
        if entry is None:
            return
        text = escape_md(line)
        if entry.activity_msg_id is None:
            msg_id = await self._gw.post(entry.topic_id, text)
            self._bk.set_activity_msg(entry.ref, msg_id)
        else:
            await self._gw.edit(entry.topic_id, entry.activity_msg_id, text)

    async def on_milestone(
        self, host: str, profile: str, local_id: int, text: str
    ) -> None:
        entry = self._bk.by_worker_task(host, profile, local_id)
        if entry is None:
            return
        await self._gw.post(entry.topic_id, escape_md(text))

    async def on_done(
        self,
        host: str,
        profile: str,
        local_id: int,
        status: str,
        summary: str,
        reset_at: float | None = None,
    ) -> None:
        entry = self._bk.by_worker_task(host, profile, local_id)
        if entry is None:
            return
        if status == "parked":
            base = (
                reset_at
                if reset_at is not None
                else self._now() + self._park_default_backoff
            )
            until = base + self._jitter()
            self._bk.park(entry.ref, until)
            when = datetime.fromtimestamp(until).strftime("%H:%M")
            await self._gw.post(
                entry.topic_id, escape_md(f"⏸ parked (usage limit) · resumes ~{when}")
            )
            return
        self._bk.set_status(entry.ref, status)
        if self._mailbox_service is not None:
            await self._mailbox_service.handle_recipient_gone(entry.ref)

    async def on_spawn_rejected(self, host: str, profile: str, reason: str) -> None:
        text = escape_md(f"spawn on {host}/{profile} rejected: {reason}")
        await self._gw.post(None, text)
