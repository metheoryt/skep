from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from fleetd.app import build_owner_middleware, format_ls
from fleetd.config import Config
from fleetd.db import Registry
from fleetd.supervisor import Supervisor


def _cfg(tmp_path):
    return Config("tok", 42, -1001, tmp_path / "repos", tmp_path / "wt",
                  claude_bin="")  # set per-test


def _gateway():
    gw = MagicMock()
    gw.create_topic = AsyncMock(return_value=555)
    gw.post = AsyncMock(return_value=9)
    gw.edit = AsyncMock()
    gw.delete_topic = AsyncMock()
    return gw


def test_format_ls_empty():
    assert "No active" in format_ls([])


def test_format_ls_lists_tasks():
    reg = Registry.open(":memory:")
    tid = reg.add_task("nix", "clean nvidia", "/wt/nix-1")
    reg.update(tid, status="running")
    out = format_ls(reg.list_active())
    assert "nix" in out
    assert str(tid) in out


def test_format_ls_escapes_markdownv2():
    reg = Registry.open(":memory:")
    tid = reg.add_task("claude-code.v2", "t", "/wt/x")
    reg.update(tid, status="running")
    out = format_ls(reg.list_active())
    assert "claude\\-code\\.v2" in out
    assert "claude-code.v2" not in out


async def test_owner_middleware_passes_owner(tmp_path):
    cfg = _cfg(tmp_path)
    mw = build_owner_middleware(Config("tok", 42, -1001, tmp_path / "repos",
                                       tmp_path / "wt", claude_bin=""))
    ev = MagicMock()
    ev.message = MagicMock()
    ev.message.from_user = MagicMock(id=42)
    ev.configure_mock(edited_message=None, channel_post=None,
                      edited_channel_post=None, callback_query=None,
                      inline_query=None, my_chat_member=None, chat_member=None)
    handler = AsyncMock(return_value="ok")

    result = await mw(handler, ev, {})

    assert handler.await_count == 1
    assert result == "ok"


async def test_owner_middleware_blocks_non_owner(tmp_path):
    cfg = _cfg(tmp_path)
    mw = build_owner_middleware(Config("tok", 42, -1001, tmp_path / "repos",
                                       tmp_path / "wt", claude_bin=""))
    ev = MagicMock()
    ev.message = MagicMock()
    ev.message.from_user = MagicMock(id=999)
    ev.configure_mock(edited_message=None, channel_post=None,
                      edited_channel_post=None, callback_query=None,
                      inline_query=None, my_chat_member=None, chat_member=None)
    handler = AsyncMock(return_value="ok")

    result = await mw(handler, ev, {})

    assert handler.await_count == 0
    assert result is None


async def test_end_to_end_spawn_with_fake_claude(tmp_path, git_repo,
                                                  fake_claude_cmd):
    import asyncio

    cfg = Config("tok", 42, -1001, git_repo.parent, tmp_path / "wt",
                 claude_bin=fake_claude_cmd)
    # repo dir must be named for the spawn arg
    repo_name = git_repo.name
    reg = Registry.open(":memory:")
    gw = _gateway()
    sup = Supervisor(cfg, reg, gw)

    tid = await sup.spawn(repo_name, "clean nvidia")
    # wait for the background event loop to finish
    for _ in range(200):
        if reg.get_task(tid).status in ("done", "failed", "killed"):
            break
        await asyncio.sleep(0.02)

    task = reg.get_task(tid)
    assert task.status == "done"
    assert task.session_id == "fake-sess"
    assert gw.create_topic.await_count == 1
    assert gw.post.await_count >= 1
