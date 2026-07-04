import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
import aiohttp

from skep import wire
from skep.config import WorkerConfig
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.router import QueenRouter
from skep.transport import SwitchableEventSink
from skep.ws_transport import QueenWsServer, WorkerWsClient


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


class FlakyInbox(RecordingInbox):
    """Inbox whose on_activity raises for a poisoned line, to exercise
    the receive loop's per-message error isolation."""

    async def on_activity(self, host, profile, local_id, line):
        if line == "boom":
            raise RuntimeError("rendering failed")
        await super().on_activity(host, profile, local_id, line)


async def test_dispatch_error_does_not_kill_connection():
    inbox = FlakyInbox()
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
                # this frame's handler raises — must not sever the socket
                await ws.send_str(wire.encode(wire.activity_msg(1, "boom")))
                # a well-formed frame afterwards must still be processed
                await ws.send_str(wire.encode(wire.activity_msg(1, "still alive")))
                for _ in range(100):
                    if ("activity", "g16", "work", 1, "still alive") in inbox.events:
                        break
                    await asyncio.sleep(0.01)
                assert not ws.closed
    finally:
        await server.close()
    assert ("activity", "g16", "work", 1, "still alive") in inbox.events
    assert not any(e[0] == "activity" and e[4] == "boom" for e in inbox.events)


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


class FakeSupervisor:
    """Stands in for Supervisor as a CommandHandler + list_active source."""

    def __init__(self, capacity_ok=True):
        self.spawned: list[tuple[str, str]] = []
        self.killed: list[int] = []
        self.panics = 0
        self._capacity_ok = capacity_ok

    def list_active(self):
        return []

    async def spawn(self, repo, task):
        from skep.supervisor import CapacityError
        if not self._capacity_ok:
            raise CapacityError("at capacity (0 running)")
        self.spawned.append((repo, task))
        return 1

    async def kill(self, task_id):
        self.killed.append(task_id)
        return True

    async def panic(self):
        self.panics += 1
        return 0


def _wcfg(url):
    from pathlib import Path
    return WorkerConfig(
        host="g16", profile="work", claude_config_dir=None,
        repos_root=Path("/tmp"), worktrees_root=Path("/tmp"),
        db_path=":memory:", queen_url=url, shared_secret="s",
    )


async def test_worker_client_dispatches_spawn_command():
    inbox = RecordingInbox()
    router = QueenRouter(Bookkeeping.open(":memory:"))
    server, url = await _serve(router, inbox)
    sup = FakeSupervisor()
    switch = SwitchableEventSink()
    client = WorkerWsClient(_wcfg(url), sup, switch, secret="s")
    try:
        async with aiohttp.ClientSession() as sess:
            task = asyncio.create_task(client.run_once(sess, url))
            # queen waits for the worker to register, then spawns
            for _ in range(100):
                try:
                    await router.cmd_spawn("g16", "work", "nix", "clean")
                    break
                except Exception:
                    await asyncio.sleep(0.01)
            for _ in range(100):
                if sup.spawned:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
    finally:
        await server.close()
    assert sup.spawned == [("nix", "clean")]


async def test_worker_client_reports_capacity_rejection():
    inbox = RecordingInbox()
    router = QueenRouter(Bookkeeping.open(":memory:"))
    server, url = await _serve(router, inbox)
    sup = FakeSupervisor(capacity_ok=False)
    switch = SwitchableEventSink()
    client = WorkerWsClient(_wcfg(url), sup, switch, secret="s")
    try:
        async with aiohttp.ClientSession() as sess:
            task = asyncio.create_task(client.run_once(sess, url))
            for _ in range(100):
                try:
                    await router.cmd_spawn("g16", "work", "nix", "clean")
                    break
                except Exception:
                    await asyncio.sleep(0.01)
            for _ in range(100):
                if any(e[0] == "spawn_rejected" for e in inbox.events):
                    break
                await asyncio.sleep(0.01)
            task.cancel()
    finally:
        await server.close()
    assert any(e[0] == "spawn_rejected" for e in inbox.events)
