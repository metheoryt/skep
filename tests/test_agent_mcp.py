from skep.agent import AgentProcess


def _argv(**kw):
    return AgentProcess("do it", "/tmp/wt", "claude", **kw)._argv()


def test_no_mcp_config_when_path_absent():
    assert "--mcp-config" not in _argv()


def test_no_allowed_tools_when_grant_absent():
    assert "--allowedTools" not in _argv()


def test_mcp_config_passed_as_path():
    argv = _argv(mcp_config_path="/wt/.skep/mcp.json")
    assert argv[argv.index("--mcp-config") + 1] == "/wt/.skep/mcp.json"
    assert "--strict-mcp-config" not in argv


def test_token_and_url_never_appear_in_argv():
    argv = _argv(mcp_config_path="/wt/.skep/mcp.json")
    joined = " ".join(argv)
    assert "Bearer" not in joined and "http://" not in joined and "127.0.0.1" not in joined


def test_allowed_tools_passed_comma_joined():
    argv = _argv(allowed_tools=["Bash", "Edit", "Write", "mcp__memory__remember"])
    expected = "Bash,Edit,Write,mcp__memory__remember"
    assert argv[argv.index("--allowedTools") + 1] == expected


def test_argv_emits_the_grant_verbatim_adding_nothing():
    # AgentProcess is not the layer that decides the grant (Supervisor is); it
    # must emit exactly the tools it was handed, injecting nothing. If it
    # silently added a tool (e.g. Read), the enumeration would be wider than
    # intended. (That Read is absent from the DESIGNED grant is pinned at
    # Supervisor level by test_base_tools_grant_write_but_not_read.)
    argv = _argv(allowed_tools=["Bash", "Edit", "Write"])
    assert argv[argv.index("--allowedTools") + 1] == "Bash,Edit,Write"


def test_append_system_prompt_coexists_with_mcp_config_and_grant():
    argv = _argv(
        append_system_prompt="## Memory",
        mcp_config_path="/wt/.skep/mcp.json",
        allowed_tools=["Bash", "mcp__memory__remember"],
    )
    assert argv[argv.index("--append-system-prompt") + 1] == "## Memory"
    assert "--mcp-config" in argv and "--allowedTools" in argv
