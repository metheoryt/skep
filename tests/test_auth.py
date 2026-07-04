import asyncio

import pytest

from skep.auth import AuthError, _proof, handshake_client, handshake_server


def _pipe():
    """Two async queues wired as a bidirectional dict channel."""
    a: asyncio.Queue = asyncio.Queue()
    b: asyncio.Queue = asyncio.Queue()

    async def send_a(m):
        await a.put(m)

    async def recv_a():
        return await b.get()

    async def send_b(m):
        await b.put(m)

    async def recv_b():
        return await a.get()

    # server uses send_a/recv_a; client uses send_b/recv_b
    return (send_a, recv_a), (send_b, recv_b)


async def test_handshake_succeeds_with_matching_secret():
    (s_send, s_recv), (c_send, c_recv) = _pipe()
    await asyncio.gather(
        handshake_server(s_send, s_recv, "secret"),
        handshake_client(c_send, c_recv, "secret"),
    )  # no exception == success


async def test_server_rejects_wrong_client_secret():
    (s_send, s_recv), (c_send, c_recv) = _pipe()
    results = await asyncio.gather(
        handshake_server(s_send, s_recv, "right"),
        handshake_client(c_send, c_recv, "wrong"),
        return_exceptions=True,
    )
    assert any(isinstance(r, AuthError) for r in results)


async def test_client_rejects_wrong_server_secret():
    (s_send, s_recv), (c_send, c_recv) = _pipe()
    results = await asyncio.gather(
        handshake_server(s_send, s_recv, "wrong"),
        handshake_client(c_send, c_recv, "right"),
        return_exceptions=True,
    )
    client_result = results[1]
    assert isinstance(client_result, AuthError)


async def test_replayed_client_proof_is_rejected():
    # Capture a valid client `auth` frame produced against server nonce "N1".
    captured: dict = {}

    async def s_send(m):
        pass

    async def s_recv():
        raise AssertionError("not used")

    async def c_send(m):
        captured.update(m)

    # Make c_recv stateful to return challenge on first call, auth_ok on second
    c_recv_calls = [0]
    async def c_recv():
        c_recv_calls[0] += 1
        if c_recv_calls[0] == 1:
            return {"t": "challenge", "nonce": "N1"}
        else:
            return {"t": "auth_ok", "proof": _proof("secret", "CN", "N1")}

    await handshake_client(c_send, c_recv, "secret",
                           nonce_factory=lambda: "CN")
    assert captured["t"] == "auth"

    # A fresh server issues a DIFFERENT nonce "N2"; replaying the old proof fails.
    sent: list = []

    async def s2_send(m):
        sent.append(m)

    async def s2_recv():
        return captured  # replayed stale auth frame

    with pytest.raises(AuthError):
        await handshake_server(s2_send, s2_recv, "secret",
                               nonce_factory=lambda: "N2")
    assert any(m.get("t") == "auth_error" for m in sent)


async def test_client_raises_on_auth_error_frame():
    # Server sends a valid challenge, then an auth_error instead of auth_ok.
    calls = [0]

    async def c_recv():
        calls[0] += 1
        if calls[0] == 1:
            return {"t": "challenge", "nonce": "N1"}
        return {"t": "auth_error"}

    async def c_send(m):
        pass

    with pytest.raises(AuthError):
        await handshake_client(c_send, c_recv, "secret",
                               nonce_factory=lambda: "CN")


async def test_server_rejects_malformed_auth_frame_and_notifies():
    sent: list = []

    async def s_send(m):
        sent.append(m)

    async def s_recv():
        return {"t": "not-auth"}

    with pytest.raises(AuthError):
        await handshake_server(s_send, s_recv, "secret",
                               nonce_factory=lambda: "N1")
    assert any(m.get("t") == "auth_error" for m in sent)
