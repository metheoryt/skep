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
from skep.ws_transport import QueenWsServer, WorkerWsClient, WsEventSink


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


class PoisonedReplayInbox(RecordingInbox):
    """Inbox whose on_task_started raises for a poisoned reconnect-replay
    entry, to exercise the register-time active_tasks replay loop's
    per-item error isolation (mirrors FlakyInbox for steady-state)."""

    async def on_task_started(self, host, profile, local_id, repo, title):
        if repo == "poison":
            raise RuntimeError("replay failed")
        await super().on_task_started(host, profile, local_id, repo, title)


async def test_reattach_replay_error_does_not_kill_connection():
    inbox = PoisonedReplayInbox()
    router = QueenRouter(Bookkeeping.open(":memory:"))
    server, url = await _serve(router, inbox)
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(url) as ws:
                await _client_handshake(ws)
                # register frame carries an active_tasks entry that makes
                # on_task_started raise during reconnect replay
                await ws.send_str(wire.encode(
                    wire.register_msg("g16", "work", "0.1.0", [
                        {"local_id": 1, "repo": "poison", "title": "bad"},
                    ])))
                # a well-formed steady-state frame afterwards must still be
                # processed — the poisoned replay must not sever the socket
                await ws.send_str(wire.encode(
                    wire.task_started_msg(2, "nix", "clean")))
                for _ in range(100):
                    if ("task_started", "g16", "work", 2, "nix", "clean") in inbox.events:
                        break
                    await asyncio.sleep(0.01)
                assert not ws.closed
    finally:
        await server.close()
    assert ("task_started", "g16", "work", 2, "nix", "clean") in inbox.events
    assert not any(e[0] == "task_started" and e[4] == "poison" for e in inbox.events)


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


async def test_worker_sends_heartbeat():
    inbox = RecordingInbox()
    router = QueenRouter(Bookkeeping.open(":memory:"))
    server, url = await _serve(router, inbox)
    sup = FakeSupervisor()
    switch = SwitchableEventSink()
    client = WorkerWsClient(_wcfg(url), sup, switch, secret="s", heartbeat=0.05)
    seen = {"beat": False}

    # Observe router.touch indirectly via the public last_seen() accessor
    # (avoids reaching into the private _last_seen dict). Registration
    # itself calls mark_online (which also sets last_seen), so the
    # baseline must be captured *after* the worker is online — otherwise
    # the registration bump alone would satisfy the assertion without any
    # application-level heartbeat frame ever being sent.
    try:
        async with aiohttp.ClientSession() as sess:
            task = asyncio.create_task(client.run_once(sess, url))
            for _ in range(100):
                if router.is_online("g16", "work"):
                    break
                await asyncio.sleep(0.02)
            assert router.is_online("g16", "work")
            before = router.last_seen("g16", "work")

            for _ in range(100):
                after = router.last_seen("g16", "work")
                if after is not None and after != before:
                    seen["beat"] = True
                    break
                await asyncio.sleep(0.02)
            task.cancel()
    finally:
        await server.close()
    assert seen["beat"]


async def test_worker_reconnects_after_drop():
    inbox = RecordingInbox()
    router = QueenRouter(Bookkeeping.open(":memory:"))
    server, url = await _serve(router, inbox)
    sup = FakeSupervisor()
    switch = SwitchableEventSink()
    client = WorkerWsClient(_wcfg(url), sup, switch, secret="s")

    connects = {"n": 0}
    orig = client.run_once

    async def counting_run_once(session, u):
        connects["n"] += 1
        if connects["n"] == 1:
            raise ConnectionError("simulated drop")
        await orig(session, u)

    client.run_once = counting_run_once  # type: ignore[method-assign]
    try:
        task = asyncio.create_task(client.run(max_backoff=0.1))
        for _ in range(200):
            if connects["n"] >= 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
    finally:
        await server.close()
    assert connects["n"] >= 2  # dropped once, reconnected


class DyingWs:
    """Stands in for a ws whose send raises once the socket is dying —
    exercises WsEventSink's send guard without a real network drop."""

    async def send_str(self, data):
        raise ConnectionResetError("socket is dying")


async def test_ws_event_sink_swallows_send_error_on_dying_socket():
    sink = WsEventSink(DyingWs())  # type: ignore[arg-type]
    # None of these must raise — a dying socket must not propagate into
    # Supervisor.run_events and mark a still-running agent's task failed.
    await sink.task_started(1, "nix", "clean")
    await sink.activity(1, "still going")
    await sink.milestone(1, "checkpoint")
    await sink.done(1, "done", "ok")


def test_task_started_msg_includes_session_local_id():
    msg = wire.task_started_msg(7, "nix", "t", 3)
    assert msg["local_id"] == 7
    assert msg["session_local_id"] == 3


def test_task_started_msg_defaults_session_local_id_none():
    msg = wire.task_started_msg(7, "nix", "t")
    assert msg["session_local_id"] is None


class CapturingWs:
    """Stands in for a ws whose send_str records the decoded outgoing frame —
    used to verify WsEventSink builds the wire message with the 4th arg,
    not just that wire.task_started_msg itself accepts it."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_str(self, data):
        self.sent.append(wire.decode(data))


async def test_ws_event_sink_forwards_session_local_id():
    ws = CapturingWs()
    sink = WsEventSink(ws)  # type: ignore[arg-type]
    await sink.task_started(1, "nix", "t", 42)
    assert ws.sent[-1]["local_id"] == 1
    assert ws.sent[-1]["session_local_id"] == 42
