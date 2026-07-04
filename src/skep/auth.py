"""Mutual HMAC challenge-response handshake.

Wire protocol (4 frames, JSON dicts with a ``"t"`` discriminator):

- ``challenge``  (server -> client): ``{"t": "challenge", "nonce": <server_nonce>}``
- ``auth``       (client -> server): ``{"t": "auth", "nonce": <client_nonce>, "proof": <hmac>}``
- ``auth_ok``    (server -> client): ``{"t": "auth_ok", "proof": <hmac>}`` on success
- ``auth_error`` (server -> client): ``{"t": "auth_error"}`` sent before the server
  raises ``AuthError``, so the client (blocked on ``recv()``) is unblocked instead
  of deadlocking, e.g. under ``asyncio.gather(..., return_exceptions=True)``.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

Send = Callable[[dict[str, Any]], Awaitable[None]]
Recv = Callable[[], Awaitable[dict[str, Any]]]


class AuthError(Exception):
    """Raised when the challenge-response handshake fails on either side."""


def _proof(secret: str, first: str, second: str) -> str:
    return hmac.new(secret.encode(), f"{first}:{second}".encode(),
                    hashlib.sha256).hexdigest()


async def handshake_server(
    send: Send, recv: Recv, secret: str, *,
    nonce_factory: Callable[[], str] = secrets.token_hex,
) -> None:
    server_nonce = nonce_factory()
    await send({"t": "challenge", "nonce": server_nonce})
    msg = await recv()
    if msg.get("t") != "auth":
        await send({"t": "auth_error"})
        raise AuthError("expected auth frame")
    client_nonce = str(msg.get("nonce", ""))
    expected = _proof(secret, server_nonce, client_nonce)
    if not hmac.compare_digest(str(msg.get("proof", "")), expected):
        await send({"t": "auth_error"})
        raise AuthError("bad client proof")
    await send({"t": "auth_ok", "proof": _proof(secret, client_nonce, server_nonce)})


async def handshake_client(
    send: Send, recv: Recv, secret: str, *,
    nonce_factory: Callable[[], str] = secrets.token_hex,
) -> None:
    msg = await recv()
    if msg.get("t") != "challenge":
        raise AuthError("expected challenge frame")
    server_nonce = str(msg.get("nonce", ""))
    client_nonce = nonce_factory()
    await send({"t": "auth", "nonce": client_nonce,
                "proof": _proof(secret, server_nonce, client_nonce)})
    reply = await recv()
    if reply.get("t") == "auth_error":
        raise AuthError("authentication rejected by server")
    if reply.get("t") != "auth_ok":
        raise AuthError("expected auth_ok frame")
    expected = _proof(secret, client_nonce, server_nonce)
    if not hmac.compare_digest(str(reply.get("proof", "")), expected):
        raise AuthError("bad server proof")
