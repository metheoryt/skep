from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    group_chat_id: int
    repos_root: Path
    worktrees_root: Path
    claude_bin: str = "claude"


def load_config(env: Mapping[str, str]) -> Config:
    return Config(
        bot_token=env["FLEETD_BOT_TOKEN"],
        owner_id=int(env["FLEETD_OWNER_ID"]),
        group_chat_id=int(env["FLEETD_GROUP_CHAT_ID"]),
        repos_root=Path(env["FLEETD_REPOS_ROOT"]),
        worktrees_root=Path(env["FLEETD_WORKTREES_ROOT"]),
        claude_bin=env.get("FLEETD_CLAUDE_BIN", "claude"),
    )
