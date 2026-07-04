import asyncio

import pytest

from fleetd.app import build_worker_and_router, parse_spawn
from fleetd.config import QueenConfig, WorkerConfig
from fleetd.db import Registry
from fleetd.queen.bookkeeping import Bookkeeping
from fleetd.queen.router import QueenRouter
from fleetd.queen.telegram_sink import QueenSink
from unittest.mock import AsyncMock, MagicMock


def test_parse_spawn_with_profile():
    assert parse_spawn("g16 --profile work nix clean the nvidia mess") == (
        "g16", "work", "nix", "clean the nvidia mess",
    )


def test_parse_spawn_default_profile():
    assert parse_spawn("g16 nix clean nvidia") == (
        "g16", "default", "nix", "clean nvidia",
    )


def test_parse_spawn_too_few_args_is_none():
    assert parse_spawn("g16 nix") is None
    assert parse_spawn("") is None


def _gateway():
    gw = MagicMock()
    gw.create_topic = AsyncMock(return_value=555)
    gw.post = AsyncMock(return_value=9)
    gw.edit = AsyncMock()
    return gw


async def test_end_to_end_spawn_with_fake_claude(tmp_path, git_repo, fake_claude_cmd):
    repo_name = git_repo.name
    wcfg = WorkerConfig(
        host="g16", profile="work", claude_config_dir=None,
        repos_root=git_repo.parent, worktrees_root=tmp_path / "wt",
        db_path=":memory:", max_concurrent=8, claude_bin=fake_claude_cmd,
    )
    gw = _gateway()
    bk = Bookkeeping.open(":memory:")
    router, supervisor = build_worker_and_router(wcfg, QueenSink(gw, bk), bk,
                                                 registry=Registry.open(":memory:"))

    await router.cmd_spawn("g16", "work", repo_name, "clean nvidia")

    for _ in range(200):
        entry = bk.by_worker_task("g16", "work", 1)
        if entry and entry.status in ("done", "failed", "killed"):
            break
        await asyncio.sleep(0.02)

    entry = bk.by_worker_task("g16", "work", 1)
    assert entry.status == "done"
    assert gw.create_topic.await_count == 1
    assert gw.post.await_count >= 1
