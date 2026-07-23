from pathlib import Path

import pytest

from skep.config import QueenConfig, WorkerConfig, load_queen_config, load_worker_config


def _worker_env():
    return {
        "SKEP_HOST": "g16",
        "SKEP_PROFILE": "work",
        "SKEP_CLAUDE_CONFIG_DIR": "/home/me/.claude-work",
        "SKEP_REPOS_ROOT": "/home/me/gh",
        "SKEP_WORKTREES_ROOT": "/home/me/.skep/wt",
        "SKEP_DB": "/home/me/.skep/work.sqlite",
    }


def _queen_env():
    return {
        "SKEP_BOT_TOKEN": "tok",
        "SKEP_OWNER_ID": "42",
        "SKEP_GROUP_CHAT_ID": "-1001",
    }


def test_load_worker_config_parses_fields():
    cfg = load_worker_config(_worker_env())
    assert cfg == WorkerConfig(
        host="g16",
        profile="work",
        claude_config_dir="/home/me/.claude-work",
        repos_root=Path("/home/me/gh"),
        worktrees_root=Path("/home/me/.skep/wt"),
        db_path="/home/me/.skep/work.sqlite",
        max_concurrent=8,
        claude_bin="claude",
    )


def test_worker_host_defaults_to_hostname(monkeypatch):
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "boxy")
    env = _worker_env()
    del env["SKEP_HOST"]
    assert load_worker_config(env).host == "boxy"


def test_worker_profile_defaults_to_default():
    env = _worker_env()
    del env["SKEP_PROFILE"]
    assert load_worker_config(env).profile == "default"


def test_worker_claude_config_dir_optional():
    env = _worker_env()
    del env["SKEP_CLAUDE_CONFIG_DIR"]
    assert load_worker_config(env).claude_config_dir is None


def test_worker_max_concurrent_override():
    env = _worker_env() | {"SKEP_MAX_CONCURRENT": "3"}
    assert load_worker_config(env).max_concurrent == 3


def test_load_queen_config_parses_fields():
    cfg = load_queen_config(_queen_env())
    assert cfg == QueenConfig(bot_token="tok", owner_id=42, group_chat_id=-1001,
                              bookkeeping_db="queen.sqlite")


def test_queen_missing_token_raises():
    env = _queen_env()
    del env["SKEP_BOT_TOKEN"]
    with pytest.raises(KeyError):
        load_queen_config(env)


def test_queen_config_network_defaults():
    cfg = load_queen_config(_queen_env())
    assert cfg.listen_host == "0.0.0.0"
    assert cfg.listen_port == 8765
    assert cfg.shared_secret == ""
    assert cfg.public_url is None
    assert cfg.advertise_mdns is True


def test_queen_config_network_overrides():
    env = _queen_env() | {
        "SKEP_LISTEN_HOST": "10.0.0.2",
        "SKEP_LISTEN_PORT": "9000",
        "SKEP_SHARED_SECRET": "s3cr3t",
        "SKEP_PUBLIC_URL": "wss://skep.cyphy.kz/ws",
        "SKEP_ADVERTISE_MDNS": "false",
    }
    cfg = load_queen_config(env)
    assert cfg.listen_host == "10.0.0.2"
    assert cfg.listen_port == 9000
    assert cfg.shared_secret == "s3cr3t"
    assert cfg.public_url == "wss://skep.cyphy.kz/ws"
    assert cfg.advertise_mdns is False


def test_worker_config_transport_defaults():
    cfg = load_worker_config(_worker_env())
    assert cfg.queen_url is None
    assert cfg.shared_secret == ""
    assert cfg.use_mdns is True


def test_worker_config_transport_overrides():
    env = _worker_env() | {
        "SKEP_QUEEN_URL": "wss://skep.cyphy.kz/ws",
        "SKEP_SHARED_SECRET": "s3cr3t",
        "SKEP_USE_MDNS": "0",
    }
    cfg = load_worker_config(env)
    assert cfg.queen_url == "wss://skep.cyphy.kz/ws"
    assert cfg.shared_secret == "s3cr3t"
    assert cfg.use_mdns is False


def _base_env():
    return {
        "SKEP_BOT_TOKEN": "t",
        "SKEP_OWNER_ID": "1",
        "SKEP_GROUP_CHAT_ID": "-100",
        "SKEP_SHARED_SECRET": "s",
    }


def test_managers_default_empty():
    cfg = load_queen_config(_base_env())
    assert cfg.managers == frozenset()
    assert cfg.mailbox_rate_limit == 20
    assert cfg.mailbox_depth_cap == 10
    assert cfg.mailbox_body_cap == 16384


def test_managers_parsed_and_overrides():
    env = _base_env() | {
        "SKEP_MANAGERS": "alice, bob ,carol",
        "SKEP_MAILBOX_RATE_LIMIT": "5",
        "SKEP_MAILBOX_RATE_WINDOW": "30",
        "SKEP_MAILBOX_DEPTH_CAP": "3",
        "SKEP_MAILBOX_DEDUPE_WINDOW": "15",
        "SKEP_MAILBOX_BODY_CAP": "1024",
    }
    cfg = load_queen_config(env)
    assert cfg.managers == frozenset({"alice", "bob", "carol"})
    assert cfg.mailbox_rate_limit == 5
    assert cfg.mailbox_rate_window == 30.0
    assert cfg.mailbox_depth_cap == 3
    assert cfg.mailbox_dedupe_window == 15.0
    assert cfg.mailbox_body_cap == 1024


def test_park_knobs_default():
    cfg = load_queen_config(_base_env())
    assert cfg.park_sweep_interval == 30.0
    assert cfg.park_default_backoff == 3600.0


def test_park_knobs_overrides():
    env = _base_env() | {
        "SKEP_PARK_SWEEP_INTERVAL": "5",
        "SKEP_PARK_DEFAULT_BACKOFF": "120",
    }
    cfg = load_queen_config(env)
    assert cfg.park_sweep_interval == 5.0
    assert cfg.park_default_backoff == 120.0


def test_memory_enabled_defaults_true():
    env = _worker_env()
    assert load_worker_config(env).memory_enabled is True


def test_memory_enabled_can_be_disabled():
    env = _worker_env() | {"SKEP_MEMORY_ENABLED": "false"}
    assert load_worker_config(env).memory_enabled is False


def test_memory_max_bytes_defaults_8192():
    env = _worker_env()
    assert load_worker_config(env).memory_max_bytes == 8192


def test_memory_max_bytes_override():
    env = _worker_env() | {"SKEP_MEMORY_MAX_BYTES": "4096"}
    assert load_worker_config(env).memory_max_bytes == 4096


def test_worker_config_agent_env_passthrough_parsed():
    cfg = load_worker_config({
        "SKEP_REPOS_ROOT": "/r", "SKEP_WORKTREES_ROOT": "/w", "SKEP_DB": ":memory:",
        "SKEP_AGENT_ENV_PASSTHROUGH": "FOO, BAR ,,BAZ",
    })
    assert cfg.agent_env_passthrough == ("FOO", "BAR", "BAZ")


def test_worker_config_agent_env_passthrough_defaults_empty():
    cfg = load_worker_config({
        "SKEP_REPOS_ROOT": "/r", "SKEP_WORKTREES_ROOT": "/w", "SKEP_DB": ":memory:",
    })
    assert cfg.agent_env_passthrough == ()
