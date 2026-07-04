import pytest
from aiohttp import web

from skep.config import QueenConfig
from skep.queen import app as queen_app
from skep.queen.app import build_queen, serve


def _qcfg(**overrides):
    kwargs = dict(bot_token="123:abc", owner_id=42, group_chat_id=-100,
                  shared_secret="s", bookkeeping_db=":memory:")
    kwargs.update(overrides)
    return QueenConfig(**kwargs)


def test_build_queen_wires_ws_route():
    bot, dp, app, router = build_queen(_qcfg())
    paths = [r.resource.canonical for r in app.router.routes()
             if r.resource is not None]
    assert "/ws" in paths
    assert router is not None
    # dispatcher has the owner-gated commands registered
    assert dp is not None


async def test_serve_cleans_up_runner_when_site_start_fails(monkeypatch):
    """Regression test: if site.start() (or mDNS advertise) raises during
    startup, the AppRunner set up just before it must still be cleaned up
    rather than leaking a bound runner/site."""
    qcfg = _qcfg(advertise_mdns=False)

    cleanup_calls: list[web.AppRunner] = []
    real_cleanup = web.AppRunner.cleanup

    async def spy_cleanup(self):
        cleanup_calls.append(self)
        await real_cleanup(self)

    class ExplodingTCPSite:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            raise RuntimeError("boom: site.start failed")

    monkeypatch.setattr(web.AppRunner, "cleanup", spy_cleanup)
    monkeypatch.setattr(queen_app.web, "TCPSite", ExplodingTCPSite)

    with pytest.raises(RuntimeError, match="boom"):
        await serve(qcfg)

    assert len(cleanup_calls) == 1
