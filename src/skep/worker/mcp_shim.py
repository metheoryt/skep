"""Per-agent MCP shim: exposes send_message / read_inbox over streamable-HTTP.

Identity (`tid`) is closed over per agent -- spoof-proof `from` (§11): the
spawned agent cannot influence which mailbox identity its messages carry.
Each agent gets its own FastMCP app on an ephemeral 127.0.0.1 port (never
exposed off-host).
"""

from __future__ import annotations

import asyncio
import hmac
import socket
from typing import Any, Callable

import uvicorn
from mcp.server.fastmcp import FastMCP

from skep.transport import MailboxClient


def _require_bearer(app: Any, token: str) -> Any:
    """Wrap an ASGI app so every HTTP request must carry
    `Authorization: Bearer <token>`; otherwise it gets 401 before reaching the
    app. Non-HTTP scopes (lifespan/websocket) pass through untouched so
    uvicorn's startup/shutdown still work.

    Raises the bar against a process that discovers another agent's ephemeral
    shim port: merely connecting no longer suffices -- the caller must present
    the per-agent token. NOTE: the token is currently handed to the agent on
    its command line (--mcp-config), so a SAME-UID sibling that reads
    /proc/<pid>/cmdline can still recover it; true per-agent isolation needs
    separate UIDs / a sandbox (tracked as an L0.2 follow-up). This guard is
    defense-in-depth that becomes fully effective under that isolation.
    """
    expected = f"Bearer {token}".encode()

    async def guarded(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            provided = headers.get(b"authorization", b"")
            if not hmac.compare_digest(provided, expected):
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"text/plain")],
                })
                await send({
                    "type": "http.response.body",
                    "body": b"unauthorized",
                })
                return
        await app(scope, receive, send)

    return guarded


def _pick_free_port(host: str) -> int:
    """Reserve an ephemeral port on `host` by binding and immediately closing.

    FastMCP's own port=0 binding doesn't surface the OS-assigned port back
    to us (uvicorn is constructed fresh, internally, inside
    run_streamable_http_async), so we pick the port ourselves up front and
    tell FastMCP to bind that exact port. Small TOCTOU risk between the
    close() here and uvicorn's bind, acceptable for a localhost-only,
    one-shot-per-agent server.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


class MailboxShim:
    def __init__(
        self,
        client: MailboxClient,
        tid: int,
        host: str = "127.0.0.1",
        token: str | None = None,
    ) -> None:
        self._client = client
        self._tid = tid
        self._host = host
        self._token = token
        self._server: FastMCP | None = None
        self._userver: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._port: int | None = None

    def _tools(self) -> dict[str, Callable[..., Any]]:
        async def send_message(
            to: str,
            subject: str,
            body: str,
            in_reply_to: int | None = None,
        ) -> dict[str, Any]:
            """Send a message to another agent (ceo / mgr:<name> / <ref>)."""
            reply = await self._client.send(
                self._tid, to, subject, body, in_reply_to)
            return {
                "ok": reply.ok,
                "message_id": reply.message_id,
                "error": reply.error,
                "status": reply.status,
            }

        async def read_inbox() -> dict[str, Any]:
            """Read and archive all unread messages addressed to you."""
            messages = await self._client.read(self._tid)
            return {"messages": messages}

        return {"send_message": send_message, "read_inbox": read_inbox}

    def _build_server(self) -> FastMCP:
        port = self._port if self._port is not None else 0
        server = FastMCP("skep-mailbox", host=self._host, port=port)
        tools = self._tools()
        server.add_tool(tools["send_message"], name="send_message")
        server.add_tool(tools["read_inbox"], name="read_inbox")
        return server

    async def start(self) -> str:
        self._port = _pick_free_port(self._host)
        self._server = self._build_server()
        app = self._server.streamable_http_app()
        if self._token is not None:
            app = _require_bearer(app, self._token)
        config = uvicorn.Config(
            app, host=self._host, port=self._port, log_level="warning")
        self._userver = uvicorn.Server(config)
        self._task = asyncio.create_task(self._userver.serve())
        # Wait until uvicorn has actually bound the socket and is accepting
        # connections (bounded), so callers never race a not-yet-listening
        # server (~5s max).
        for _ in range(500):
            if self._task.done():
                exc = self._task.exception()  # serve() exited before ready
                await self.stop()
                raise RuntimeError(
                    "mailbox shim server exited during startup") from exc
            if self._userver.started:
                return self._base_url()
            await asyncio.sleep(0.01)
        await self.stop()  # timeout: don't leak a slow-but-live server
        raise RuntimeError("mailbox shim server failed to start")

    def _base_url(self) -> str:
        assert self._server is not None
        port = self._server.settings.port
        return f"http://{self._host}:{port}/mcp"

    async def stop(self) -> None:
        if self._userver is not None:
            # Graceful shutdown: uvicorn closes its listening socket before
            # serve() returns, unlike cancelling the task (which interrupts
            # the ASGI lifespan mid-shutdown and leaks the fd/port).
            self._userver.should_exit = True
        if self._task is not None:
            try:
                await self._task
            except (Exception, SystemExit):
                # uvicorn calls sys.exit() on a bind collision, so serve() can
                # finish with SystemExit (a BaseException, not Exception).
                # Swallow it here -- but NOT bare BaseException: CancelledError
                # must still propagate so cooperative shutdown isn't broken.
                pass
        self._userver = None
        self._task = None
        self._server = None
