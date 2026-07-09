from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from typing import Any, Protocol

import aiohttp
from aiohttp import web

from skep import wire
from skep.auth import AuthError, handshake_client, handshake_server
from skep.config import WorkerConfig
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.mailbox import MailboxService, agent_sender
from skep.queen.router import QueenRouter
from skep.supervisor import CapacityError, Supervisor
from skep.transport import (
    MailboxUnavailable,
    QueenInbox,
    SendReply,
    SwitchableEventSink,
    SwitchableMailboxClient,
)

logger = logging.getLogger(__name__)

WORKER_VERSION = "0.1.0"


class RemoteWorker:
    """A CommandHandler that forwards queen commands to one worker's socket."""

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self._ws = ws

    async def spawn(self, repo: str, task: str) -> int:
        await self._ws.send_str(wire.encode(wire.spawn_msg(repo, task)))
        return 0

    async def kill(self, task_id: int) -> bool:
        await self._ws.send_str(wire.encode(wire.kill_msg(task_id)))
        return True

    async def panic(self) -> int:
        await self._ws.send_str(wire.encode(wire.panic_msg()))
        return 1


class QueenWsServer:
    def __init__(
        self,
        router: QueenRouter,
        inbox: QueenInbox,
        secret: str,
        *,
        heartbeat: float = 20.0,
        bookkeeping: Bookkeeping | None = None,
        mailbox_service: MailboxService | None = None,
    ) -> None:
        self._router = router
        self._inbox = inbox
        self._secret = secret
        self._heartbeat = heartbeat
        self._bk = bookkeeping
        self._mailbox_service = mailbox_service

    def attach(self, app: web.Application, path: str = "/ws") -> None:
        app.router.add_get(path, self._handle)

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=self._heartbeat)
        await ws.prepare(request)

        async def send(m: dict[str, Any]) -> None:
            await ws.send_str(wire.encode(m))

        async def recv() -> dict[str, Any]:
            msg = await ws.receive()
            if msg.type != web.WSMsgType.TEXT:
                raise AuthError("connection closed during handshake")
            return wire.decode(msg.data)

        try:
            await handshake_server(send, recv, self._secret)
            reg = await recv()
        except (AuthError, ValueError):
            await ws.close()
            return ws
        if reg.get("t") != wire.REGISTER:
            await ws.close()
            return ws

        host = str(reg["host"])
        profile = str(reg["profile"])
        remote = RemoteWorker(ws)
        self._router.register(host, profile, remote)
        self._router.mark_online(host, profile)
        try:
            for t in reg.get("active_tasks", []):
                try:
                    await self._inbox.on_task_started(
                        host,
                        profile,
                        int(t["local_id"]),
                        str(t["repo"]),
                        str(t["title"]),
                    )
                except Exception:
                    logger.exception(
                        "error replaying active task from %s/%s: %r", host, profile, t
                    )
                    continue

            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    await self._dispatch(host, profile, ws, wire.decode(msg.data))
                except Exception:
                    logger.exception(
                        "error dispatching message from %s/%s: %r",
                        host,
                        profile,
                        msg.data,
                    )
        finally:
            self._router.detach_if_current(host, profile, remote)
        return ws

    async def _dispatch(
        self, host: str, profile: str, ws: web.WebSocketResponse, msg: dict[str, Any]
    ) -> None:
        t = msg.get("t")
        if t == wire.HEARTBEAT:
            self._router.touch(host, profile)
        elif t == wire.TASK_STARTED:
            await self._inbox.on_task_started(
                host, profile, int(msg["local_id"]), str(msg["repo"]), str(msg["title"])
            )
        elif t == wire.ACTIVITY:
            await self._inbox.on_activity(
                host, profile, int(msg["local_id"]), str(msg["line"])
            )
        elif t == wire.MILESTONE:
            await self._inbox.on_milestone(
                host, profile, int(msg["local_id"]), str(msg["text"])
            )
        elif t == wire.DONE:
            await self._inbox.on_done(
                host,
                profile,
                int(msg["local_id"]),
                str(msg["status"]),
                str(msg["summary"]),
            )
        elif t == wire.SPAWN_REJECTED:
            await self._inbox.on_spawn_rejected(host, profile, str(msg["reason"]))
        elif t == wire.MAILBOX_SEND:
            await self._dispatch_mailbox_send(host, profile, ws, msg)
        elif t == wire.INBOX_READ:
            await self._dispatch_inbox_read(host, profile, ws, msg)

    async def _dispatch_mailbox_send(
        self, host: str, profile: str, ws: web.WebSocketResponse, msg: dict[str, Any]
    ) -> None:
        req_id = str(msg["req_id"])
        if self._mailbox_service is None or self._bk is None:
            await ws.send_str(
                wire.encode(
                    wire.mailbox_ack_msg(
                        req_id, False, None, "mailbox unavailable", "rejected"
                    )
                )
            )
            return
        try:
            sender = agent_sender(self._bk, host, profile, int(msg["tid"]))
        except ValueError as exc:
            await ws.send_str(
                wire.encode(
                    wire.mailbox_ack_msg(req_id, False, None, str(exc), "rejected")
                )
            )
            return
        res = await self._mailbox_service.handle_send(
            sender=sender,
            to=str(msg["to"]),
            subject=str(msg["subject"]),
            body=str(msg["body"]),
            in_reply_to=msg.get("in_reply_to"),
        )
        await ws.send_str(
            wire.encode(
                wire.mailbox_ack_msg(
                    req_id, res.ok, res.message_id, res.error, res.status
                )
            )
        )

    async def _dispatch_inbox_read(
        self, host: str, profile: str, ws: web.WebSocketResponse, msg: dict[str, Any]
    ) -> None:
        req_id = str(msg["req_id"])
        if self._mailbox_service is None or self._bk is None:
            await ws.send_str(wire.encode(wire.inbox_reply_msg(req_id, [])))
            return
        try:
            sender = agent_sender(self._bk, host, profile, int(msg["tid"]))
        except ValueError:
            await ws.send_str(wire.encode(wire.inbox_reply_msg(req_id, [])))
            return
        msgs = await self._mailbox_service.handle_read(sender)
        await ws.send_str(
            wire.encode(
                wire.inbox_reply_msg(
                    req_id,
                    [
                        {
                            "id": m.id,
                            "sender": m.sender,
                            "subject": m.subject,
                            "body": m.body,
                            "created_at": m.created_at,
                            "in_reply_to": m.in_reply_to,
                        }
                        for m in msgs
                    ],
                )
            )
        )


class WsEventSink:
    """EventSink that serialises each domain event to a JSON frame.

    Sends are guarded: a dying socket must never propagate into the
    Supervisor's event loop and mark a still-running agent's task as
    failed (design §6.4 — agents keep running, only reporting pauses).
    The receive-loop's own drop detection (run_once's finally clearing
    switch.target) is what handles cleanup, not an exception from here.
    """

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws

    async def _send(self, msg: dict[str, Any]) -> None:
        try:
            await self._ws.send_str(wire.encode(msg))
        except (aiohttp.ClientError, ConnectionError, RuntimeError) as exc:
            logger.warning("dropped event send on dying socket: %r", exc)

    async def task_started(self, local_id: int, repo: str, title: str) -> None:
        await self._send(wire.task_started_msg(local_id, repo, title))

    async def activity(self, local_id: int, line: str) -> None:
        await self._send(wire.activity_msg(local_id, line))

    async def milestone(self, local_id: int, text: str) -> None:
        await self._send(wire.milestone_msg(local_id, text))

    async def done(self, local_id: int, status: str, summary: str) -> None:
        await self._send(wire.done_msg(local_id, status, summary))


class WorkerWsClient:
    """Connects out to the queen: handshake -> register -> command loop.

    Holds the Supervisor's SwitchableEventSink and swaps its target in/out
    for the lifetime of one connection, so a dropped link pauses reporting
    without killing running agents (design §6.4).
    """

    def __init__(
        self,
        config: WorkerConfig,
        supervisor: Supervisor,
        switch: SwitchableEventSink,
        secret: str,
        *,
        heartbeat: float = 20.0,
        mailbox_switch: SwitchableMailboxClient | None = None,
    ) -> None:
        self._cfg = config
        self._sup = supervisor
        self._switch = switch
        self._secret = secret
        self._heartbeat = heartbeat
        self._mailbox_switch = mailbox_switch

    def _active_payload(self) -> list[dict[str, Any]]:
        return [
            {"local_id": t.id, "repo": t.repo, "title": t.task}
            for t in self._sup.list_active()
            if t.id is not None
        ]

    async def _heartbeat_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            await asyncio.sleep(self._heartbeat)
            active = self._active_payload()
            remaining = max(0, self._cfg.max_concurrent - len(active))
            await ws.send_str(wire.encode(wire.heartbeat_msg(active, remaining)))

    async def run_once(self, session: aiohttp.ClientSession, url: str) -> None:
        async with session.ws_connect(url, heartbeat=self._heartbeat) as ws:

            async def send(m: dict[str, Any]) -> None:
                await ws.send_str(wire.encode(m))

            async def recv() -> dict[str, Any]:
                msg = await ws.receive()
                if msg.type != aiohttp.WSMsgType.TEXT:
                    raise AuthError("connection closed during handshake")
                return wire.decode(msg.data)

            await handshake_client(send, recv, self._secret)
            await send(
                wire.register_msg(
                    self._cfg.host,
                    self._cfg.profile,
                    WORKER_VERSION,
                    self._active_payload(),
                )
            )
            self._switch.target = WsEventSink(ws)
            mailbox_client = WsMailboxClient(ws)
            if self._mailbox_switch is not None:
                self._mailbox_switch.set_target(mailbox_client)
            hb = asyncio.create_task(self._heartbeat_loop(ws))
            try:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        decoded = wire.decode(msg.data)
                        if decoded.get("t") in (wire.MAILBOX_ACK, wire.INBOX_REPLY):
                            mailbox_client.resolve(decoded)
                            continue
                        await self._on_command(ws, decoded)
                    except Exception:
                        logger.exception(
                            "error handling command from queen: %r", msg.data
                        )
            finally:
                hb.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hb
                self._switch.target = None
                mailbox_client.fail_all("link down")
                if self._mailbox_switch is not None:
                    self._mailbox_switch.set_target(None)

    async def run(self, *, max_backoff: float = 30.0, _once: bool = False) -> None:
        backoff = 0.5
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    url = self._cfg.queen_url or ""
                    await self.run_once(session, url)
                backoff = 0.5  # clean close -> reset
            except (aiohttp.ClientError, ConnectionError, AuthError, OSError) as exc:
                logger.warning("queen connection failed, will retry: %r", exc)
            if _once:
                return
            await asyncio.sleep(backoff)
            backoff = min(max_backoff, backoff * 2)

    async def _on_command(
        self, ws: aiohttp.ClientWebSocketResponse, msg: dict[str, Any]
    ) -> None:
        t = msg.get("t")
        if t == wire.SPAWN:
            try:
                await self._sup.spawn(str(msg["repo"]), str(msg["task"]))
            except CapacityError as exc:
                await ws.send_str(wire.encode(wire.spawn_rejected_msg(str(exc))))
        elif t == wire.KILL:
            await self._sup.kill(int(msg["task_id"]))
        elif t == wire.PANIC:
            await self._sup.panic()


class _MailboxWs(Protocol):
    """Structural subset of a websocket the mailbox client needs to send on."""

    async def send_str(self, data: str) -> None: ...


class WsMailboxClient:
    """Worker-side MailboxClient with req_id/Future correlation over one WS.

    `send`/`read` register a Future keyed by a locally-minted req_id, send
    the frame, then await the Future with a bounded timeout so a queen that
    never answers can't hang the caller forever. `resolve` (called from the
    WorkerWsClient receive loop for MAILBOX_ACK/INBOX_REPLY frames) completes
    the matching Future. `fail_all` (called on link-down) resolves every
    still-pending Future to a retryable error — the other half of the
    no-deadlock guarantee.
    """

    def __init__(self, ws: _MailboxWs, timeout: float = 30.0) -> None:
        self._ws = ws
        self._timeout = timeout
        self._counter = itertools.count(1)
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    def _new_req(self) -> tuple[str, asyncio.Future[dict[str, Any]]]:
        req_id = f"m{next(self._counter)}"
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        return req_id, fut

    def resolve(self, msg: dict[str, Any]) -> None:
        fut = self._pending.pop(msg.get("req_id", ""), None)
        if fut is not None and not fut.done():
            fut.set_result(msg)

    def fail_all(self, reason: str) -> None:
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_result({"__error__": reason})

    async def send(
        self,
        tid: int,
        to: str,
        subject: str,
        body: str,
        in_reply_to: int | None,
    ) -> SendReply:
        req_id, fut = self._new_req()
        await self._ws.send_str(
            wire.encode(
                wire.mailbox_send_msg(req_id, tid, to, subject, body, in_reply_to)
            )
        )
        try:
            msg = await asyncio.wait_for(fut, self._timeout)
        except TimeoutError:
            self._pending.pop(req_id, None)
            return SendReply(
                False, None, "queen did not respond (retryable)", "rejected"
            )
        if "__error__" in msg:
            return SendReply(False, None, f"{msg['__error__']} (retryable)", "rejected")
        return SendReply(msg["ok"], msg["message_id"], msg["error"], msg["status"])

    async def read(self, tid: int) -> list[dict[str, Any]]:
        req_id, fut = self._new_req()
        await self._ws.send_str(wire.encode(wire.inbox_read_msg(req_id, tid)))
        try:
            msg = await asyncio.wait_for(fut, self._timeout)
        except TimeoutError:
            self._pending.pop(req_id, None)
            raise MailboxUnavailable("queen did not respond (retryable)") from None
        if "__error__" in msg:
            raise MailboxUnavailable(msg["__error__"])
        return msg["messages"]
