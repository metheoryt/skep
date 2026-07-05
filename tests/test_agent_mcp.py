import json

from skep.agent import AgentProcess


def _argv(**kw):
    ap = AgentProcess("do it", "/tmp/wt", "claude", **kw)
    return ap._argv()


def test_no_mcp_config_when_url_absent():
    argv = _argv()
    assert "--mcp-config" not in argv


def test_mcp_config_injected_with_bearer():
    argv = _argv(mcp_url="http://127.0.0.1:5000/mcp", mcp_token="secret")
    i = argv.index("--mcp-config")
    cfg = json.loads(argv[i + 1])
    server = cfg["mcpServers"]["mailbox"]
    assert server["type"] == "http"
    assert server["url"] == "http://127.0.0.1:5000/mcp"
    assert server["headers"]["Authorization"] == "Bearer secret"
    assert "--strict-mcp-config" not in argv


def test_mcp_config_without_token_omits_headers():
    argv = _argv(mcp_url="http://127.0.0.1:5000/mcp")
    i = argv.index("--mcp-config")
    cfg = json.loads(argv[i + 1])
    assert "headers" not in cfg["mcpServers"]["mailbox"]
