import pytest

from skep import discovery
from skep.config import WorkerConfig
from pathlib import Path


def _wcfg(**kw):
    base = dict(
        host="g16", profile="work", claude_config_dir=None,
        repos_root=Path("/tmp"), worktrees_root=Path("/tmp"),
        db_path=":memory:",
    )
    base.update(kw)
    return WorkerConfig(**base)


async def test_resolve_prefers_explicit_url(monkeypatch):
    called = {"browsed": False}

    async def fake_browse(timeout=3.0):
        called["browsed"] = True
        return "ws://discovered:8765/ws"

    monkeypatch.setattr(discovery, "browse", fake_browse)
    cfg = _wcfg(queen_url="wss://skep.cyphy.kz/ws")
    assert await discovery.resolve_queen_url(cfg) == "wss://skep.cyphy.kz/ws"
    assert called["browsed"] is False  # no mDNS when URL is explicit


async def test_resolve_uses_mdns_when_no_url(monkeypatch):
    async def fake_browse(timeout=3.0):
        return "ws://discovered:8765/ws"

    monkeypatch.setattr(discovery, "browse", fake_browse)
    cfg = _wcfg(queen_url=None, use_mdns=True)
    assert await discovery.resolve_queen_url(cfg) == "ws://discovered:8765/ws"


async def test_resolve_returns_none_when_mdns_disabled_and_no_url():
    cfg = _wcfg(queen_url=None, use_mdns=False)
    assert await discovery.resolve_queen_url(cfg) is None


@pytest.mark.mdns
async def test_advertise_and_browse_roundtrip():
    handle = await discovery.advertise("127.0.0.1", 8765)
    try:
        url = await discovery.browse(timeout=3.0)
    finally:
        await handle.close()
    assert url is not None
    assert url.endswith(":8765/ws")
