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
    h.spawn.assert_awaited_once_with("nix", "clean nvidia")


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
