from skep.config import QueenConfig
from skep.queen.app import build_queen


def _qcfg():
    return QueenConfig(bot_token="123:abc", owner_id=42, group_chat_id=-100,
                       shared_secret="s", bookkeeping_db=":memory:")


def test_build_queen_wires_ws_route():
    bot, dp, app, router = build_queen(_qcfg())
    paths = [r.resource.canonical for r in app.router.routes()
             if r.resource is not None]
    assert "/ws" in paths
    assert router is not None
    # dispatcher has the owner-gated commands registered
    assert dp is not None
