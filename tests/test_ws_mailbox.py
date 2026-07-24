import asyncio

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from skep import wire
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.mailbox import Message, SendResult
from skep.queen.router import QueenRouter
from skep.ws_transport import QueenWsServer, WsMailboxClient


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send_str(self, s):
        self.sent.append(s)


async def test_send_registers_future_and_resolves_on_ack():
    ws = _FakeWs()
    client = WsMailboxClient(ws)

    async def _drive():
        # wait until a frame is sent, then feed the ack
        while not ws.sent:
            await asyncio.sleep(0)
        frame = wire.decode(ws.sent[-1])
        assert frame["t"] == wire.MAILBOX_SEND
        assert frame["tid"] == 7
        client.resolve(wire.decode(wire.encode(
            wire.mailbox_ack_msg(frame["req_id"], True, 42, None, "delivered"))))

    driver = asyncio.create_task(_drive())
    reply = await client.send(tid=7, to="ceo", subject="s", body="b",
                              in_reply_to=None)
    await driver
    assert reply.ok and reply.message_id == 42 and reply.status == "delivered"


async def test_read_resolves_on_reply():
    ws = _FakeWs()
    client = WsMailboxClient(ws)

    async def _drive():
        while not ws.sent:
            await asyncio.sleep(0)
        frame = wire.decode(ws.sent[-1])
        assert frame["t"] == wire.INBOX_READ
        client.resolve(wire.inbox_reply_msg(
            frame["req_id"], [{"id": 1, "sender": "ceo", "subject": "s",
                               "body": "b", "created_at": 1.0,
                               "in_reply_to": None}]))

    driver = asyncio.create_task(_drive())
    msgs = await client.read(tid=7)
    await driver
    assert msgs[0]["subject"] == "s"


async def test_link_down_fails_pending():
    ws = _FakeWs()
    client = WsMailboxClient(ws)
    task = asyncio.create_task(
        client.send(tid=1, to="ceo", subject="s", body="b", in_reply_to=None))
    await asyncio.sleep(0)
    client.fail_all("link down")
    reply = await task
    assert not reply.ok and "link down" in reply.error


# --- Queen-side _dispatch wiring: MAILBOX_SEND / INBOX_READ over a real WS ---


class _NullInbox:
    async def on_task_started(self, host, profile, local_id, repo, title):
        pass

    async def on_activity(self, host, profile, local_id, line):
        pass

    async def on_milestone(self, host, profile, local_id, text):
        pass

    async def on_done(self, host, profile, local_id, status, summary, reset_at=None):
        pass

    async def on_spawn_rejected(
        self, host, profile, reason, action="spawn", origin=None
    ):
        pass


class _FakeMailboxService:
    def __init__(self, send_result=None, read_result=None):
        self.send_calls = []
        self.read_calls = []
        self._send_result = send_result or SendResult(True, 7, None, "delivered")
        self._read_result = read_result if read_result is not None else []

    async def handle_send(self, sender, to, subject, body, in_reply_to=None):
        self.send_calls.append((sender, to, subject, body, in_reply_to))
        return self._send_result

    async def handle_read(self, recipient):
        self.read_calls.append(recipient)
        return self._read_result


async def _serve(router, inbox, secret="s", bookkeeping=None, mailbox_service=None):
    app = web.Application()
    QueenWsServer(router, inbox, secret,
                 bookkeeping=bookkeeping, mailbox_service=mailbox_service).attach(app)
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


async def test_queen_dispatches_mailbox_send_to_service():
    bk = Bookkeeping.open(":memory:")
    bk.add("g16", "work", 1, "nix", "clean", topic_id=1)
    svc = _FakeMailboxService()
    router = QueenRouter(bk)
    server, url = await _serve(router, _NullInbox(), bookkeeping=bk, mailbox_service=svc)
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(url) as ws:
                await _client_handshake(ws)
                await ws.send_str(wire.encode(
                    wire.register_msg("g16", "work", "0.1.0", [])))
                await ws.send_str(wire.encode(
                    wire.mailbox_send_msg("r1", 1, "ceo", "s", "b", None)))
                got = wire.decode((await ws.receive()).data)
    finally:
        await server.close()
    assert got["t"] == wire.MAILBOX_ACK
    assert got["req_id"] == "r1"
    assert got["ok"] is True
    assert got["message_id"] == 7
    assert svc.send_calls == [("1", "ceo", "s", "b", None)]


async def test_queen_mailbox_send_unknown_sender_gets_error_ack():
    bk = Bookkeeping.open(":memory:")
    svc = _FakeMailboxService()
    router = QueenRouter(bk)
    server, url = await _serve(router, _NullInbox(), bookkeeping=bk, mailbox_service=svc)
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(url) as ws:
                await _client_handshake(ws)
                await ws.send_str(wire.encode(
                    wire.register_msg("g16", "work", "0.1.0", [])))
                await ws.send_str(wire.encode(
                    wire.mailbox_send_msg("r2", 99, "ceo", "s", "b", None)))
                got = wire.decode((await ws.receive()).data)
    finally:
        await server.close()
    assert got["t"] == wire.MAILBOX_ACK
    assert got["req_id"] == "r2"
    assert got["ok"] is False
    assert svc.send_calls == []


async def test_queen_mailbox_send_without_service_gets_error_ack():
    bk = Bookkeeping.open(":memory:")
    router = QueenRouter(bk)
    server, url = await _serve(router, _NullInbox(), bookkeeping=bk, mailbox_service=None)
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(url) as ws:
                await _client_handshake(ws)
                await ws.send_str(wire.encode(
                    wire.register_msg("g16", "work", "0.1.0", [])))
                await ws.send_str(wire.encode(
                    wire.mailbox_send_msg("r3", 1, "ceo", "s", "b", None)))
                got = wire.decode((await ws.receive()).data)
    finally:
        await server.close()
    assert got["t"] == wire.MAILBOX_ACK
    assert got["ok"] is False


async def test_queen_dispatches_inbox_read_to_service():
    bk = Bookkeeping.open(":memory:")
    bk.add("g16", "work", 1, "nix", "clean", topic_id=1)
    msg = Message(id=5, sender="ceo", recipient="1", subject="hi", body="b",
                  created_at=1.0, in_reply_to=None, hops=0, status="read",
                  dead_letter_reason=None)
    svc = _FakeMailboxService(read_result=[msg])
    router = QueenRouter(bk)
    server, url = await _serve(router, _NullInbox(), bookkeeping=bk, mailbox_service=svc)
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(url) as ws:
                await _client_handshake(ws)
                await ws.send_str(wire.encode(
                    wire.register_msg("g16", "work", "0.1.0", [])))
                await ws.send_str(wire.encode(wire.inbox_read_msg("r4", 1)))
                got = wire.decode((await ws.receive()).data)
    finally:
        await server.close()
    assert got["t"] == wire.INBOX_REPLY
    assert got["req_id"] == "r4"
    assert got["messages"] == [{"id": 5, "sender": "ceo", "subject": "hi",
                                "body": "b", "created_at": 1.0,
                                "in_reply_to": None}]
    assert svc.read_calls == ["1"]


async def test_queen_inbox_read_without_service_replies_empty():
    bk = Bookkeeping.open(":memory:")
    router = QueenRouter(bk)
    server, url = await _serve(router, _NullInbox(), bookkeeping=bk, mailbox_service=None)
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(url) as ws:
                await _client_handshake(ws)
                await ws.send_str(wire.encode(
                    wire.register_msg("g16", "work", "0.1.0", [])))
                await ws.send_str(wire.encode(wire.inbox_read_msg("r5", 1)))
                got = wire.decode((await ws.receive()).data)
    finally:
        await server.close()
    assert got["t"] == wire.INBOX_REPLY
    assert got["messages"] == []


async def test_queen_inbox_read_unknown_sender_replies_empty():
    bk = Bookkeeping.open(":memory:")
    svc = _FakeMailboxService()
    router = QueenRouter(bk)
    server, url = await _serve(router, _NullInbox(), bookkeeping=bk, mailbox_service=svc)
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(url) as ws:
                await _client_handshake(ws)
                await ws.send_str(wire.encode(
                    wire.register_msg("g16", "work", "0.1.0", [])))
                await ws.send_str(wire.encode(wire.inbox_read_msg("r6", 99)))
                got = wire.decode((await ws.receive()).data)
    finally:
        await server.close()
    assert got["t"] == wire.INBOX_REPLY
    assert got["messages"] == []
