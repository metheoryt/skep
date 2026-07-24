"""The queen's auto-resume sweep (A3).

A parked session carries a POSIX wall-clock `parked_until`. A periodic loop on
the queen resumes every due row whose worker is online, with no human in it.
Edges fall out of re-evaluation: an offline or full worker is simply retried on
the next tick.
"""

import asyncio

from aiohttp import web

from skep.config import QueenConfig
from skep.queen.app import build_queen
from skep.queen.assembly import _install_park_sweep, _park_sweep_loop
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.router import QueenRouter
from skep.supervisor import CapacityError


class _Handler:
    def __init__(self, fail=None):
        self.resumed = []
        self._fail = fail

    async def spawn(self, *a, **k):
        return 0

    async def kill(self, *a, **k):
        return True

    async def panic(self):
        return 0

    async def resume(self, sid, model=None):
        self.resumed.append(sid)
        if self._fail is not None:
            raise self._fail


async def _one_pass(bk, router, now=200.0):
    """Run exactly one sweep pass, then cancel the loop in its sleep."""
    task = asyncio.create_task(
        _park_sweep_loop(bk, router, interval=3600.0, now=lambda: now)
    )
    await asyncio.sleep(0)  # let the pass run
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _parked(bk, until=100.0, host="h", profile="p", local_id=1):
    ref = bk.add(host, profile, local_id, "r", "t", topic_id=1)
    bk.park(ref, until=until)
    return ref


async def test_sweep_resumes_due_parked_on_online_worker():
    bk = Bookkeeping.open(":memory:")
    _parked(bk)
    router = QueenRouter(bk)
    h = _Handler()
    router.register("h", "p", h)
    router.mark_online("h", "p")

    await _one_pass(bk, router)

    assert h.resumed == [1]


async def test_sweep_skips_offline_worker():
    bk = Bookkeeping.open(":memory:")
    _parked(bk)
    router = QueenRouter(bk)
    h = _Handler()
    router.register("h", "p", h)  # registered but NOT marked online

    await _one_pass(bk, router)

    assert h.resumed == []


async def test_sweep_leaves_a_not_yet_due_session_parked():
    bk = Bookkeeping.open(":memory:")
    _parked(bk, until=500.0)
    router = QueenRouter(bk)
    h = _Handler()
    router.register("h", "p", h)
    router.mark_online("h", "p")

    await _one_pass(bk, router, now=200.0)

    assert h.resumed == []


async def test_sweep_survives_a_failing_entry_and_keeps_going():
    """A full worker (CapacityError) or any other per-entry blow-up must not
    take the loop down, nor stop the entries behind it."""
    bk = Bookkeeping.open(":memory:")
    _parked(bk, host="full", local_id=11)
    _parked(bk, host="boom", local_id=22)
    _parked(bk, host="ok", local_id=33)
    router = QueenRouter(bk)
    handlers = {
        "full": _Handler(fail=CapacityError("at capacity")),
        "boom": _Handler(fail=RuntimeError("worker exploded")),
        "ok": _Handler(),
    }
    for host, handler in handlers.items():
        router.register(host, "p", handler)
        router.mark_online(host, "p")

    await _one_pass(bk, router)

    # the entry behind the two bad ones still got its resume
    assert handlers["ok"].resumed == [33]
    # and both bad entries were attempted; neither killed the pass
    assert handlers["full"].resumed == [11]
    assert handlers["boom"].resumed == [22]


async def test_install_park_sweep_runs_under_apprunner_and_stops_cleanly():
    bk = Bookkeeping.open(":memory:")
    _parked(bk, until=0.0)  # due against real wall-clock time
    router = QueenRouter(bk)
    h = _Handler()
    router.register("h", "p", h)
    router.mark_online("h", "p")

    app = web.Application()
    _install_park_sweep(app, bk, router, interval=0.01)
    runner = web.AppRunner(app)
    await runner.setup()  # cleanup_ctx startup -> the loop begins
    await asyncio.sleep(0.02)
    await runner.cleanup()  # cancels the loop; must not raise

    assert h.resumed  # the installed loop really swept


def _qcfg(**overrides):
    kwargs = dict(bot_token="123:abc", owner_id=42, group_chat_id=-100,
                  shared_secret="s", bookkeeping_db=":memory:")
    kwargs.update(overrides)
    return QueenConfig(**kwargs)


def test_build_queen_installs_the_sweep_only_when_the_interval_is_positive():
    off = build_queen(_qcfg(park_sweep_interval=0.0))[2]
    on = build_queen(_qcfg(park_sweep_interval=30.0))[2]
    assert len(on.cleanup_ctx) == len(off.cleanup_ctx) + 1
