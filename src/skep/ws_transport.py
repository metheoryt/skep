from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from aiohttp import web

from skep import wire
from skep.auth import AuthError, handshake_client, handshake_server
from skep.config import WorkerConfig
from skep.queen.router import QueenRouter
from skep.supervisor import CapacityError, Supervisor
from skep.transport import QueenInbox, SwitchableEventSink

logger = logging.getLogger(__name__)

WORKER_VERSION = "0.1.0"


class RemoteWorker:
    """A CommandHandler that forwards queen commands to one worker's socket."""

    def __init__(self, ws: web.WebSocketResponse):
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
    def __init__(self, router: QueenRouter, inbox: QueenInbox, secret: str,
                 *, heartbeat: float = 20.0):
        self._router = router
        self._inbox = inbox
        self._secret = secret
        self._heartbeat = heartbeat

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
        self._router.register(host, profile, RemoteWorker(ws))
        self._router.mark_online(host, profile)
        try:
            for t in reg.get("active_tasks", []):
                await self._inbox.on_task_started(
                    host, profile, int(t["local_id"]), str(t["repo"]), str(t["title"]))

            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    await self._dispatch(host, profile, wire.decode(msg.data))
                except Exception:
                    logger.exception(
                        "error dispatching message from %s/%s: %r",
                        host, profile, msg.data)
        finally:
            self._router.mark_offline(host, profile)
            self._router.unregister(host, profile)
        return ws

    async def _dispatch(self, host: str, profile: str,
                        msg: dict[str, Any]) -> None:
        t = msg.get("t")
        if t == wire.HEARTBEAT:
            self._router.touch(host, profile)
        elif t == wire.TASK_STARTED:
            await self._inbox.on_task_started(
                host, profile, int(msg["local_id"]), str(msg["repo"]), str(msg["title"]))
        elif t == wire.ACTIVITY:
            await self._inbox.on_activity(
                host, profile, int(msg["local_id"]), str(msg["line"]))
        elif t == wire.MILESTONE:
            await self._inbox.on_milestone(
                host, profile, int(msg["local_id"]), str(msg["text"]))
        elif t == wire.DONE:
            await self._inbox.on_done(
                host, profile, int(msg["local_id"]),
                str(msg["status"]), str(msg["summary"]))
        elif t == wire.SPAWN_REJECTED:
            await self._inbox.on_spawn_rejected(host, profile, str(msg["reason"]))


class WsEventSink:
    """EventSink that serialises each domain event to a JSON frame."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse):
        self._ws = ws

    async def task_started(self, local_id: int, repo: str, title: str) -> None:
        await self._ws.send_str(wire.encode(wire.task_started_msg(local_id, repo, title)))

    async def activity(self, local_id: int, line: str) -> None:
        await self._ws.send_str(wire.encode(wire.activity_msg(local_id, line)))

    async def milestone(self, local_id: int, text: str) -> None:
        await self._ws.send_str(wire.encode(wire.milestone_msg(local_id, text)))

    async def done(self, local_id: int, status: str, summary: str) -> None:
        await self._ws.send_str(wire.encode(wire.done_msg(local_id, status, summary)))


class WorkerWsClient:
    """Connects out to the queen: handshake -> register -> command loop.

    Holds the Supervisor's SwitchableEventSink and swaps its target in/out
    for the lifetime of one connection, so a dropped link pauses reporting
    without killing running agents (design §6.4).
    """

    def __init__(self, config: WorkerConfig, supervisor: Supervisor,
                 switch: SwitchableEventSink, secret: str,
                 *, heartbeat: float = 20.0):
        self._cfg = config
        self._sup = supervisor
        self._switch = switch
        self._secret = secret
        self._heartbeat = heartbeat

    def _active_payload(self) -> list[dict[str, Any]]:
        return [{"local_id": t.id, "repo": t.repo, "title": t.task}
                for t in self._sup.list_active() if t.id is not None]

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
            await send(wire.register_msg(
                self._cfg.host, self._cfg.profile, WORKER_VERSION,
                self._active_payload()))
            self._switch.target = WsEventSink(ws)
            hb = asyncio.create_task(self._heartbeat_loop(ws))
            try:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    await self._on_command(ws, wire.decode(msg.data))
            finally:
                hb.cancel()
                self._switch.target = None

    async def _on_command(self, ws: aiohttp.ClientWebSocketResponse,
                          msg: dict[str, Any]) -> None:
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
