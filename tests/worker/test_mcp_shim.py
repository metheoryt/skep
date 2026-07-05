import asyncio

import pytest

from skep.worker.mcp_shim import MailboxShim
from skep.transport import SendReply


class _FakeClient:
    def __init__(self):
        self.sent = []
        self.reads = []

    async def send(self, tid, to, subject, body, in_reply_to):
        self.sent.append((tid, to, subject, body, in_reply_to))
        return SendReply(True, 1, None, "delivered")

    async def read(self, tid):
        self.reads.append(tid)
        return [{"id": 1, "sender": "ceo", "subject": "s", "body": "b",
                 "created_at": 1.0, "in_reply_to": None}]


async def test_send_message_tool_binds_tid_and_forwards():
    client = _FakeClient()
    shim = MailboxShim(client, tid=9)
    result = await shim._tools()["send_message"](
        to="ceo", subject="s", body="b")
    # tid comes from the shim closure, NOT tool input
    assert client.sent == [(9, "ceo", "s", "b", None)]
    assert result["ok"] is True
    assert result["status"] == "delivered"


async def test_read_inbox_tool_binds_tid_and_forwards():
    client = _FakeClient()
    shim = MailboxShim(client, tid=9)
    result = await shim._tools()["read_inbox"]()
    assert client.reads == [9]
    assert result["messages"][0]["subject"] == "s"


async def test_send_message_reports_rejection():
    class _Reject:
        async def send(self, *a, **k):
            return SendReply(False, None, "unknown manager 'x'", "rejected")

        async def read(self, tid):
            return []

    shim = MailboxShim(_Reject(), tid=1)
    result = await shim._tools()["send_message"](
        to="mgr:x", subject="s", body="b")
    assert result["ok"] is False
    assert "unknown manager" in result["error"]


def test_build_server_constructs_without_raising():
    """Catches SDK API-name errors at unit-test time (no live server needed)."""
    client = _FakeClient()
    shim = MailboxShim(client, tid=1)
    server = shim._build_server()
    assert server is not None


async def test_stop_swallows_systemexit_from_serve_task():
    """uvicorn calls sys.exit() on a bind collision, so the serve() task can
    finish with SystemExit -- a BaseException, not Exception. When start()
    detects the early exit and calls stop(), stop() awaits that already-done
    task; the stored SystemExit must not escape and crash the caller's
    teardown (e.g. Supervisor). Modeled as a done awaitable holding SystemExit,
    exactly the surface stop() awaits."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[None] = loop.create_future()
    fut.set_exception(SystemExit(1))

    shim = MailboxShim(_FakeClient(), tid=1)
    shim._task = fut  # type: ignore[assignment]

    await shim.stop()  # awaiting the done task must NOT re-raise SystemExit

    assert shim._task is None
    assert shim._server is None


async def test_stop_does_not_swallow_cancellation():
    """stop() catches uvicorn's SystemExit but must NOT eat CancelledError --
    swallowing cancellation (a bare `except BaseException`) would break
    cooperative shutdown."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[None] = loop.create_future()
    fut.cancel()

    shim = MailboxShim(_FakeClient(), tid=1)
    shim._task = fut  # type: ignore[assignment]

    with pytest.raises(asyncio.CancelledError):
        await shim.stop()


async def _post_status(url, headers=None):
    """POST to the shim's /mcp endpoint and return the HTTP status. The body
    is intentionally not a valid MCP frame -- we only care whether the bearer
    guard let the request through (any non-401) or blocked it (401)."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers or {}, json={}) as resp:
            return resp.status


async def test_shim_requires_bearer_token_when_configured():
    """A co-located agent that port-scans another agent's shim must be turned
    away: only the holder of the per-agent token may drive the tools."""
    shim = MailboxShim(_FakeClient(), tid=1, token="s3cret-token")
    url = await shim.start()
    try:
        assert await _post_status(url) == 401  # no Authorization header
        assert await _post_status(
            url, {"Authorization": "Bearer wrong"}) == 401
        assert await _post_status(
            url, {"Authorization": "s3cret-token"}) == 401  # missing "Bearer "
        # correct token -> auth passes; request reaches the MCP app (which may
        # then reject the malformed body, but NOT with 401)
        assert await _post_status(
            url, {"Authorization": "Bearer s3cret-token"}) != 401
    finally:
        await shim.stop()


async def test_shim_without_token_does_not_require_auth():
    """Back-compat: a shim built with no token accepts unauthenticated calls
    (the token=None default preserves pre-hardening behavior)."""
    shim = MailboxShim(_FakeClient(), tid=1)
    url = await shim.start()
    try:
        assert await _post_status(url) != 401
    finally:
        await shim.stop()


async def test_start_binds_then_stop_releases_port():
    """start() must not return until uvicorn is accepting connections, and
    stop() must fully release the listening socket (not just cancel the
    task) so the port can be immediately rebound without SO_REUSEADDR."""
    import socket

    shim = MailboxShim(_FakeClient(), tid=1)
    url = await shim.start()
    port = int(url.rsplit(":", 1)[1].split("/")[0])

    # Server is accepting connections right after start() returns.
    with socket.create_connection(("127.0.0.1", port), timeout=2):
        pass

    await shim.stop()

    # Port is truly free again: a fresh bind (no SO_REUSEADDR) must succeed.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
    finally:
        s.close()


async def test_failed_start_cleans_up_and_does_not_leak(monkeypatch):
    """A start() that fails must tear itself down (no leaked task/server).

    We force the failure via uvicorn.Server.serve() itself raising, rather
    than pre-occupying the port: uvicorn's real bind-failure path calls
    sys.exit() inside the server task, and asyncio re-raises SystemExit out
    of the event loop instead of surfacing it as a normal task exception --
    that would crash the whole test process, not just this test. Patching
    serve() exercises the exact same code path in start() (early task
    completion with an exception, detected via self._task.done()) without
    that hazard.
    """
    import uvicorn
    import pytest

    async def _boom_serve(self, sockets=None):
        raise RuntimeError("simulated bind failure")

    monkeypatch.setattr(uvicorn.Server, "serve", _boom_serve)

    shim = MailboxShim(_FakeClient(), tid=1)
    with pytest.raises(RuntimeError, match="exited during startup"):
        await shim.start()

    # shim tore down its own partial state -- no leaked task or server.
    assert shim._task is None
    assert shim._userver is None
    assert shim._server is None
