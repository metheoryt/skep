from __future__ import annotations

from typing import Any

from aiohttp import web

from skep import wire
from skep.auth import AuthError, handshake_server
from skep.queen.router import QueenRouter
from skep.transport import QueenInbox


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
        for t in reg.get("active_tasks", []):
            await self._inbox.on_task_started(
                host, profile, int(t["local_id"]), str(t["repo"]), str(t["title"]))

        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                await self._dispatch(host, profile, wire.decode(msg.data))
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
