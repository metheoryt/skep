from pathlib import Path

import pytest

from fleetd.config import Config, load_config


def _base_env():
    return {
        "FLEETD_BOT_TOKEN": "tok",
        "FLEETD_OWNER_ID": "42",
        "FLEETD_GROUP_CHAT_ID": "-1001",
        "FLEETD_REPOS_ROOT": "/home/me/gh",
        "FLEETD_WORKTREES_ROOT": "/home/me/.fleetd/worktrees",
    }


def test_load_config_parses_all_fields():
    cfg = load_config(_base_env())
    assert cfg == Config(
        bot_token="tok",
        owner_id=42,
        group_chat_id=-1001,
        repos_root=Path("/home/me/gh"),
        worktrees_root=Path("/home/me/.fleetd/worktrees"),
        claude_bin="claude",
    )


def test_load_config_claude_bin_override():
    env = _base_env() | {"FLEETD_CLAUDE_BIN": "/opt/claude"}
    assert load_config(env).claude_bin == "/opt/claude"


def test_load_config_missing_required_raises():
    env = _base_env()
    del env["FLEETD_BOT_TOKEN"]
    with pytest.raises(KeyError):
        load_config(env)


def test_config_is_frozen():
    cfg = load_config(_base_env())
    with pytest.raises(Exception):
        cfg.owner_id = 1  # type: ignore[misc]
