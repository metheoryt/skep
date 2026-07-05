"""CEO redelivery sweep wiring (L0.1 hardening).

The queen runs a periodic background sweep that retries pending CEO mail, so
a message whose first push failed is eventually delivered even without any
further CEO traffic to trigger an on-send redelivery.
"""

import asyncio

from aiohttp import web

from skep.queen.app import _ceo_retry_loop, _install_ceo_retry


class _Svc:
    def __init__(self):
        self.calls = 0

    async def redeliver_ceo(self):
        self.calls += 1


async def test_ceo_retry_loop_calls_redeliver_periodically():
    svc = _Svc()
    task = asyncio.create_task(_ceo_retry_loop(svc, interval=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert svc.calls >= 2


async def test_ceo_retry_loop_survives_a_failing_sweep():
    class _Flaky:
        def __init__(self):
            self.calls = 0

        async def redeliver_ceo(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")

    svc = _Flaky()
    task = asyncio.create_task(_ceo_retry_loop(svc, interval=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # first sweep raised, loop kept going
    assert svc.calls >= 2


async def test_install_ceo_retry_runs_under_apprunner_and_stops_cleanly():
    svc = _Svc()
    app = web.Application()
    _install_ceo_retry(app, svc, interval=0.01)
    runner = web.AppRunner(app)
    await runner.setup()      # triggers cleanup_ctx startup -> loop begins
    await asyncio.sleep(0.05)
    await runner.cleanup()    # cancels the loop; must not raise
    assert svc.calls >= 2
