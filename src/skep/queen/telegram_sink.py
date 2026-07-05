from __future__ import annotations

from skep.formatting import escape_md
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.mailbox import MailboxService
from skep.telegram_gw import Gateway


class QueenSink:
    """Implements QueenInbox: renders worker domain events into Telegram topics."""

    def __init__(self, gateway: Gateway, bookkeeping: Bookkeeping,
                 mailbox_service: MailboxService | None = None):
        self._gw = gateway
        self._bk = bookkeeping
        self._mailbox_service = mailbox_service

    async def on_task_started(self, host: str, profile: str, local_id: int,
                              repo: str, title: str) -> None:
        if self._bk.by_worker_task(host, profile, local_id) is not None:
            return  # re-attach: worker re-registered an already-known task
        topic_id = await self._gw.create_topic(f"{host}·{profile}·{repo}")
        self._bk.add(host, profile, local_id, repo, title, topic_id)

    async def on_activity(self, host: str, profile: str, local_id: int,
                          line: str) -> None:
        entry = self._bk.by_worker_task(host, profile, local_id)
        if entry is None:
            return
        text = escape_md(line)
        if entry.activity_msg_id is None:
            msg_id = await self._gw.post(entry.topic_id, text)
            self._bk.set_activity_msg(entry.ref, msg_id)
        else:
            await self._gw.edit(entry.topic_id, entry.activity_msg_id, text)

    async def on_milestone(self, host: str, profile: str, local_id: int,
                           text: str) -> None:
        entry = self._bk.by_worker_task(host, profile, local_id)
        if entry is None:
            return
        await self._gw.post(entry.topic_id, escape_md(text))

    async def on_done(self, host: str, profile: str, local_id: int,
                      status: str, summary: str) -> None:
        entry = self._bk.by_worker_task(host, profile, local_id)
        if entry is None:
            return
        self._bk.set_status(entry.ref, status)
        if self._mailbox_service is not None:
            await self._mailbox_service.handle_recipient_gone(entry.ref)

    async def on_spawn_rejected(self, host: str, profile: str,
                                reason: str) -> None:
        text = escape_md(f"spawn on {host}/{profile} rejected: {reason}")
        await self._gw.post(None, text)
