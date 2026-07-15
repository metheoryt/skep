import os

import pytest

INTEGRATION = os.environ.get("SKEP_RUN_INTEGRATION") == "1"


@pytest.mark.skipif(not INTEGRATION, reason="needs a real claude profile")
async def test_real_claude_runs_a_tool_under_scrubbed_env(tmp_path):
    from skep.agent import AgentProcess
    ap = AgentProcess(
        task_text=(
            "Run the shell command `echo SKEP_OK` using the Bash tool, "
            "then stop."
        ),
        cwd=tmp_path, claude_bin="claude",
        config_dir=os.path.expanduser("~/.claude"),
        allowed_tools=["Bash"],
    )
    await ap.start()
    saw_result = saw_tool = result_error = False
    async for ev in ap.events():
        if ev.kind == "tool_use":
            saw_tool = True
        if ev.kind == "result":
            saw_result = True
            result_error = getattr(ev, "is_error", False)
    assert saw_result and saw_tool and not result_error, ap.stderr_text[-800:]


def test_keepset_documents_shell_and_proxy():
    """Always-on guard that the spike-(a) keep-set additions stay."""
    from skep.agent import _CORE_ENV_KEYS, _OPTIONAL_ENV_KEYS
    assert "SHELL" in _CORE_ENV_KEYS
    assert "HTTPS_PROXY" in _OPTIONAL_ENV_KEYS
    assert "NO_PROXY" in _OPTIONAL_ENV_KEYS
