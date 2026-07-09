from skep.agent import AgentProcess


def _argv(**kw):
    return AgentProcess("do it", "/tmp/wt", "claude", **kw)._argv()


def test_no_append_system_prompt_when_absent():
    assert "--append-system-prompt" not in _argv()


def test_append_system_prompt_passed_verbatim():
    argv = _argv(append_system_prompt="## Memory\nrecall stuff\n")
    i = argv.index("--append-system-prompt")
    assert argv[i + 1] == "## Memory\nrecall stuff\n"


def test_append_system_prompt_coexists_with_mcp_config():
    argv = _argv(
        append_system_prompt="## Memory",
        mcp_url="http://127.0.0.1:5000/mcp",
        mcp_token="secret",
    )
    assert "--append-system-prompt" in argv
    assert "--mcp-config" in argv
