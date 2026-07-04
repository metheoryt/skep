import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
import aiohttp

from skep import wire
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.router import QueenRouter
from skep.ws_transport import QueenWsServer


class RecordingInbox:
    def __init__(self):
        self.events: list[tuple] = []

    async def on_task_started(self, host, profile, local_id, repo, title):
        self.events.append(("task_started", host, profile, local_id, repo, title))

    async def on_activity(self, host, profile, local_id, line):
        self.events.append(("activity", host, profile, local_id, line))

    async def on_milestone(self, host, profile, local_id, text):
        self.events.append(("milestone", host, profile, local_id, text))

    async def on_done(self, host, profile, local_id, status, summary):
        self.events.append(("done", host, profile, local_id, status, summary))

    async def on_spawn_rejected(self, host, profile, reason):
        self.events.append(("spawn_rejected", host, profile, reason))


async def _serve(router, inbox, secret="s"):
    app = web.Application()
    QueenWsServer(router, inbox, secret).attach(app)
    server = TestServer(app)
    await server.start_server()
    return server, f"ws://127.0.0.1:{server.port}/ws"


async def _client_handshake(ws, secret="s"):
    from skep.auth import handshake_client

    async def send(m):
        await ws.send_str(wire.encode(m))

    async def recv():
        msg = await ws.receive()
        return wire.decode(msg.data)

    await handshake_client(send, recv, secret)


async def test_register_then_event_reaches_inbox():
    inbox = RecordingInbox()
    router = QueenRouter(Bookkeeping.open(":memory:"))
    server, url = await _serve(router, inbox)
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(url) as ws:
                await _client_handshake(ws)
                await ws.send_str(wire.encode(
                    wire.register_msg("g16", "work", "0.1.0", [])))
                await ws.send_str(wire.encode(
                    wire.task_started_msg(1, "nix", "clean")))
                await ws.send_str(wire.encode(wire.activity_msg(1, "hi")))
                for _ in range(100):
                    if len(inbox.events) >= 2:
                        break
                    await asyncio.sleep(0.01)
    finally:
        await server.close()
    assert ("task_started", "g16", "work", 1, "nix", "clean") in inbox.events
    assert ("activity", "g16", "work", 1, "hi") in inbox.events


async def test_register_makes_worker_routable():
    inbox = RecordingInbox()
    router = QueenRouter(Bookkeeping.open(":memory:"))
    server, url = await _serve(router, inbox)
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(url) as ws:
                await _client_handshake(ws)
                await ws.send_str(wire.encode(
                    wire.register_msg("g16", "work", "0.1.0", [])))
                # wait until routable
                for _ in range(100):
                    try:
                        await router.cmd_spawn("g16", "work", "nix", "task")
                        break
                    except Exception:
                        await asyncio.sleep(0.01)
                # queen -> worker command frame should arrive
                got = wire.decode((await ws.receive()).data)
                assert got == wire.spawn_msg("nix", "task")
    finally:
        await server.close()


async def test_wrong_secret_is_rejected():
    inbox = RecordingInbox()
    router = QueenRouter(Bookkeeping.open(":memory:"))
    server, url = await _serve(router, inbox, secret="right")
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(url) as ws:
                from skep.auth import AuthError
                with pytest.raises(AuthError):
                    await _client_handshake(ws, secret="wrong")
    finally:
        await server.close()
