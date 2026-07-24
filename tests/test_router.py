from unittest.mock import AsyncMock

import pytest

from skep.queen.bookkeeping import Bookkeeping
from skep.queen.router import QueenRouter, UnknownWorker


def _handler():
    h = AsyncMock()
    return h


async def test_spawn_routes_to_registered_worker():
    bk = Bookkeeping.open(":memory:")
    router = QueenRouter(bk)
    h = _handler()
    router.register("g16", "work", h)
    await router.cmd_spawn("g16", "work", "nix", "clean nvidia")
    h.spawn.assert_awaited_once_with("nix", "clean nvidia", None)


async def test_cmd_spawn_forwards_roots():
    bk = Bookkeeping.open(":memory:")
    router = QueenRouter(bk)
    handler = AsyncMock()
    router.register("g16", "work", handler)
    roots = [{"name": "nix", "mode": "new", "access": "rw"}]

    await router.cmd_spawn("g16", "work", "nix", "t", roots=roots)

    handler.spawn.assert_awaited_once_with("nix", "t", roots)


async def test_spawn_unknown_worker_raises():
    router = QueenRouter(Bookkeeping.open(":memory:"))
    with pytest.raises(UnknownWorker):
        await router.cmd_spawn("g16", "work", "nix", "t")


async def test_kill_routes_by_ref():
    bk = Bookkeeping.open(":memory:")
    router = QueenRouter(bk)
    h = _handler()
    router.register("g16", "work", h)
    ref = bk.add("g16", "work", 5, "nix", "t", topic_id=1)
    assert await router.cmd_kill(ref) is True
    h.kill.assert_awaited_once_with(5)


async def test_kill_unknown_ref_returns_false():
    router = QueenRouter(Bookkeeping.open(":memory:"))
    assert await router.cmd_kill(999) is False


async def test_cmd_resume_routes_to_worker():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("h", "p", 1, "r", "t", topic_id=1)
    bk.park(ref, until=100.0)
    router = QueenRouter(bk)
    handler = _handler()
    router.register("h", "p", handler)
    ok = await router.cmd_resume(ref)
    assert ok is True
    handler.resume.assert_awaited_once_with(1, model=None, origin=None)


async def test_cmd_resume_forwards_origin():
    """The sweep tags its dispatches so the queen can keep the resulting
    rejections off the owner's Telegram; a human's /resume passes nothing."""
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("h", "p", 1, "r", "t", topic_id=1)
    bk.park(ref, until=100.0)
    router = QueenRouter(bk)
    handler = _handler()
    router.register("h", "p", handler)
    await router.cmd_resume(ref, origin="sweep")
    handler.resume.assert_awaited_once_with(1, model=None, origin="sweep")


async def test_cmd_resume_unknown_ref_is_false():
    router = QueenRouter(Bookkeeping.open(":memory:"))
    assert await router.cmd_resume(999) is False


async def test_cmd_resume_skips_running_entry():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("h", "p", 1, "r", "t", topic_id=1)  # status defaults to running
    router = QueenRouter(bk)
    handler = _handler()
    router.register("h", "p", handler)
    ok = await router.cmd_resume(ref)
    assert ok is False
    handler.resume.assert_not_awaited()


async def test_cmd_resume_no_worker_registered_is_false():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("h", "p", 1, "r", "t", topic_id=1)
    bk.park(ref, until=100.0)
    router = QueenRouter(bk)  # no handler registered for (h, p)
    assert await router.cmd_resume(ref) is False


async def test_panic_hits_all_workers():
    router = QueenRouter(Bookkeeping.open(":memory:"))
    h1, h2 = _handler(), _handler()
    router.register("g16", "work", h1)
    router.register("g16", "personal", h2)
    assert await router.cmd_panic() == 2
    h1.panic.assert_awaited_once()
    h2.panic.assert_awaited_once()


def test_format_ls_empty():
    assert "No active" in QueenRouter(Bookkeeping.open(":memory:")).format_ls()


def test_format_ls_lists_active_with_ref_host_profile():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("g16", "work", 5, "nix", "t", topic_id=1)
    out = QueenRouter(bk).format_ls()
    assert str(ref) in out
    assert "g16" in out and "work" in out and "nix" in out


def test_presence_online_offline_touch():
    r = QueenRouter(Bookkeeping.open(":memory:"), now=lambda: 100.0)
    assert r.is_online("g16", "work") is False
    r.mark_online("g16", "work")
    assert r.is_online("g16", "work") is True
    r.mark_offline("g16", "work")
    assert r.is_online("g16", "work") is False


def test_detach_if_current_ignores_stale_handler():
    router = QueenRouter(Bookkeeping.open(":memory:"))
    a, b = _handler(), _handler()
    # A registers and comes online.
    router.register("g16", "work", a)
    router.mark_online("g16", "work")
    # Reconnect: B replaces A in the registry (simulating a live reconnect
    # racing A's slow-to-notice drop) and comes online too.
    router.register("g16", "work", b)
    router.mark_online("g16", "work")

    # A's belated cleanup must be a no-op: B is still the live handler.
    assert router.detach_if_current("g16", "work", a) is False
    assert router.is_online("g16", "work") is True
    assert router._workers[("g16", "work")] is b

    # B's own cleanup does detach, since B is still current.
    assert router.detach_if_current("g16", "work", b) is True
    assert router.is_online("g16", "work") is False
    assert ("g16", "work") not in router._workers


async def test_format_ls_marks_detached():
    bk = Bookkeeping.open(":memory:")
    bk.add("g16", "work", 1, "nix", "clean", topic_id=5)
    r = QueenRouter(bk)
    # not online -> detached
    # NOTE: asserting "detached" rather than "(detached)" — the marker is
    # MarkdownV2-escaped as "\(detached\)", so the escape backslash before
    # the closing paren makes the literal "(detached)" substring impossible
    # to match; "detached" is the substring that's actually present.
    assert "detached" in r.format_ls()
    r.mark_online("g16", "work")
    assert "detached" not in r.format_ls()
