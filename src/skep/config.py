from __future__ import annotations

import socket
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class WorkerConfig:
    host: str
    profile: str
    claude_config_dir: str | None
    repos_root: Path
    worktrees_root: Path
    db_path: str
    max_concurrent: int = 8
    queen_url: str | None = None
    shared_secret: str = ""
    use_mdns: bool = True
    claude_bin: str = "claude"
    memory_enabled: bool = True


@dataclass(frozen=True)
class QueenConfig:
    bot_token: str
    owner_id: int
    group_chat_id: int
    listen_host: str = "0.0.0.0"
    listen_port: int = 8765
    shared_secret: str = ""
    public_url: str | None = None
    advertise_mdns: bool = True
    bookkeeping_db: str = "queen.sqlite"
    # Mailbox loop-prevention knobs
    managers: frozenset[str] = frozenset()
    mailbox_rate_limit: int = 20
    mailbox_rate_window: float = 60.0
    mailbox_depth_cap: int = 10
    mailbox_dedupe_window: float = 60.0
    mailbox_body_cap: int = 16384
    # How often the queen retries pending CEO mail whose push failed (0 = off)
    mailbox_ceo_retry_interval: float = 30.0


def load_worker_config(env: Mapping[str, str]) -> WorkerConfig:
    return WorkerConfig(
        host=env.get("SKEP_HOST") or socket.gethostname(),
        profile=env.get("SKEP_PROFILE", "default"),
        claude_config_dir=env.get("SKEP_CLAUDE_CONFIG_DIR"),
        repos_root=Path(env["SKEP_REPOS_ROOT"]),
        worktrees_root=Path(env["SKEP_WORKTREES_ROOT"]),
        db_path=env["SKEP_DB"],
        max_concurrent=int(env.get("SKEP_MAX_CONCURRENT", "8")),
        queen_url=env.get("SKEP_QUEEN_URL"),
        shared_secret=env.get("SKEP_SHARED_SECRET", ""),
        use_mdns=_as_bool(env.get("SKEP_USE_MDNS"), True),
        claude_bin=env.get("SKEP_CLAUDE_BIN", "claude"),
        memory_enabled=_as_bool(env.get("SKEP_MEMORY_ENABLED"), True),
    )


def load_queen_config(env: Mapping[str, str]) -> QueenConfig:
    managers_raw = env.get("SKEP_MANAGERS", "")
    managers = frozenset(
        name.strip() for name in managers_raw.split(",") if name.strip()
    )
    return QueenConfig(
        bot_token=env["SKEP_BOT_TOKEN"],
        owner_id=int(env["SKEP_OWNER_ID"]),
        group_chat_id=int(env["SKEP_GROUP_CHAT_ID"]),
        listen_host=env.get("SKEP_LISTEN_HOST", "0.0.0.0"),
        listen_port=int(env.get("SKEP_LISTEN_PORT", "8765")),
        shared_secret=env.get("SKEP_SHARED_SECRET", ""),
        public_url=env.get("SKEP_PUBLIC_URL"),
        advertise_mdns=_as_bool(env.get("SKEP_ADVERTISE_MDNS"), True),
        bookkeeping_db=env.get("SKEP_QUEEN_DB", "queen.sqlite"),
        managers=managers,
        mailbox_rate_limit=int(env.get("SKEP_MAILBOX_RATE_LIMIT", "20")),
        mailbox_rate_window=float(env.get("SKEP_MAILBOX_RATE_WINDOW", "60")),
        mailbox_depth_cap=int(env.get("SKEP_MAILBOX_DEPTH_CAP", "10")),
        mailbox_dedupe_window=float(env.get("SKEP_MAILBOX_DEDUPE_WINDOW", "60")),
        mailbox_body_cap=int(env.get("SKEP_MAILBOX_BODY_CAP", "16384")),
        mailbox_ceo_retry_interval=float(
            env.get("SKEP_MAILBOX_CEO_RETRY_INTERVAL", "30")
        ),
    )
