from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


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
    )


def load_queen_config(env: Mapping[str, str]) -> QueenConfig:
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
    )
