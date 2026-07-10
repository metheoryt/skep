import json

from skep.agent import AgentProcess


def _argv(**kw):
    return AgentProcess("do it", "/tmp/wt", "claude", **kw)._argv()


def _mailbox(url="http://127.0.0.1:5000/mcp", token="secret"):
    server = {"type": "http", "url": url}
    if token is not None:
        server["headers"] = {"Authorization": f"Bearer {token}"}
    return server


def _memory(repo="/repos/skep"):
    return {
        "type": "stdio",
        "command": "python",
        "args": ["-m", "skep.worker.memory_shim", repo],
    }


def test_no_mcp_config_when_servers_absent():
    assert "--mcp-config" not in _argv()


def test_no_allowed_tools_when_grant_absent():
    assert "--allowedTools" not in _argv()


def test_mailbox_only():
    argv = _argv(mcp_servers={"mailbox": _mailbox()})
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    assert set(cfg["mcpServers"]) == {"mailbox"}
    assert cfg["mcpServers"]["mailbox"]["headers"]["Authorization"] == "Bearer secret"


def test_memory_only_when_mailbox_is_off():
    # Memory must work with no mailbox_client -- the shape the old
    # mcp_url-keyed code could not express.
    argv = _argv(mcp_servers={"memory": _memory()})
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    assert set(cfg["mcpServers"]) == {"memory"}
    assert cfg["mcpServers"]["memory"]["type"] == "stdio"


def test_both_servers_coexist_in_one_map():
    # spec §5.2: a stdio entry and an http entry in the one mcpServers map.
    argv = _argv(mcp_servers={"mailbox": _mailbox(), "memory": _memory()})
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    assert set(cfg["mcpServers"]) == {"mailbox", "memory"}
    assert cfg["mcpServers"]["mailbox"]["type"] == "http"
    assert cfg["mcpServers"]["memory"]["type"] == "stdio"


def test_memory_key_makes_the_grant_name_correct():
    # `mcp__memory__remember` is only correct because the key is "memory".
    argv = _argv(mcp_servers={"memory": _memory()})
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    assert "memory" in cfg["mcpServers"]


def test_allowed_tools_passed_comma_joined():
    argv = _argv(allowed_tools=["Bash", "Edit", "Write", "mcp__memory__remember"])
    expected = "Bash,Edit,Write,mcp__memory__remember"
    assert argv[argv.index("--allowedTools") + 1] == expected


def test_read_is_not_in_any_grant_we_pass():
    # spec §2.5: Read needs no grant. Passing it would be harmless but is not
    # the design; this test pins the decision so a drive-by cannot widen it.
    argv = _argv(allowed_tools=["Bash", "Edit", "Write"])
    assert "Read" not in argv[argv.index("--allowedTools") + 1].split(",")


def test_append_system_prompt_coexists_with_mcp_config_and_grant():
    argv = _argv(
        append_system_prompt="## Memory",
        mcp_servers={"memory": _memory()},
        allowed_tools=["Bash", "mcp__memory__remember"],
    )
    assert argv[argv.index("--append-system-prompt") + 1] == "## Memory"
    assert "--mcp-config" in argv and "--allowedTools" in argv
