from pathlib import Path

import pytest

from fleetd.config import QueenConfig, WorkerConfig, load_queen_config, load_worker_config


def _worker_env():
    return {
        "FLEETD_HOST": "g16",
        "FLEETD_PROFILE": "work",
        "FLEETD_CLAUDE_CONFIG_DIR": "/home/me/.claude-work",
        "FLEETD_REPOS_ROOT": "/home/me/gh",
        "FLEETD_WORKTREES_ROOT": "/home/me/.fleetd/wt",
        "FLEETD_DB": "/home/me/.fleetd/work.sqlite",
    }


def _queen_env():
    return {
        "FLEETD_BOT_TOKEN": "tok",
        "FLEETD_OWNER_ID": "42",
        "FLEETD_GROUP_CHAT_ID": "-1001",
    }


def test_load_worker_config_parses_fields():
    cfg = load_worker_config(_worker_env())
    assert cfg == WorkerConfig(
        host="g16",
        profile="work",
        claude_config_dir="/home/me/.claude-work",
        repos_root=Path("/home/me/gh"),
        worktrees_root=Path("/home/me/.fleetd/wt"),
        db_path="/home/me/.fleetd/work.sqlite",
        max_concurrent=8,
        claude_bin="claude",
    )


def test_worker_host_defaults_to_hostname(monkeypatch):
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "boxy")
    env = _worker_env()
    del env["FLEETD_HOST"]
    assert load_worker_config(env).host == "boxy"


def test_worker_profile_defaults_to_default():
    env = _worker_env()
    del env["FLEETD_PROFILE"]
    assert load_worker_config(env).profile == "default"


def test_worker_claude_config_dir_optional():
    env = _worker_env()
    del env["FLEETD_CLAUDE_CONFIG_DIR"]
    assert load_worker_config(env).claude_config_dir is None


def test_worker_max_concurrent_override():
    env = _worker_env() | {"FLEETD_MAX_CONCURRENT": "3"}
    assert load_worker_config(env).max_concurrent == 3


def test_load_queen_config_parses_fields():
    cfg = load_queen_config(_queen_env())
    assert cfg == QueenConfig(bot_token="tok", owner_id=42, group_chat_id=-1001,
                              bookkeeping_db="queen.sqlite")


def test_queen_missing_token_raises():
    env = _queen_env()
    del env["FLEETD_BOT_TOKEN"]
    with pytest.raises(KeyError):
        load_queen_config(env)
