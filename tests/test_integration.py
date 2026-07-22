import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
import aiohttp

from skep.app import build_worker_and_router, parse_spawn
from skep.config import QueenConfig, WorkerConfig
from skep.db import Registry
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.router import QueenRouter
from skep.queen.telegram_sink import QueenSink
from skep.ws_transport import QueenWsServer, WorkerWsClient
from skep.transport import SwitchableEventSink
from skep.supervisor import Supervisor
from unittest.mock import AsyncMock, MagicMock


def test_parse_spawn_with_profile():
    assert parse_spawn("g16 --profile work nix clean the nvidia mess") == (
        "g16", "work", "nix", False, "clean the nvidia mess",
    )


def test_parse_spawn_default_profile():
    assert parse_spawn("g16 nix clean nvidia") == (
        "g16", "default", "nix", False, "clean nvidia",
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


async def _start_queen(secret="s"):
    gw = _gateway()
    bk = Bookkeeping.open(":memory:")
    router = QueenRouter(bk)
    sink = QueenSink(gw, bk)
    app = web.Application()
    QueenWsServer(router, sink, secret).attach(app)
    server = TestServer(app)
    await server.start_server()
    url = f"ws://127.0.0.1:{server.port}/ws"
    return server, url, router, bk, gw


def _worker(wcfg, url, secret="s"):
    registry = Registry.open(":memory:")
    switch = SwitchableEventSink()
    sup = Supervisor(wcfg, registry, switch)
    client = WorkerWsClient(wcfg, sup, switch, secret)
    return sup, client


async def test_two_worker_spawn_and_ls(tmp_path, git_repo, fake_claude_cmd):
    repo_name = git_repo.name
    server, url, router, bk, gw = await _start_queen()

    def wcfg(profile):
        return WorkerConfig(
            host="g16", profile=profile, claude_config_dir=None,
            repos_root=git_repo.parent, worktrees_root=tmp_path / f"wt-{profile}",
            db_path=":memory:", queen_url=url, shared_secret="s",
            claude_bin=fake_claude_cmd,
        )

    _sup_w, client_w = _worker(wcfg("work"), url)
    _sup_p, client_p = _worker(wcfg("personal"), url)
    try:
        async with aiohttp.ClientSession() as s1, aiohttp.ClientSession() as s2:
            t1 = asyncio.create_task(client_w.run_once(s1, url))
            t2 = asyncio.create_task(client_p.run_once(s2, url))

            async def spawn_when_ready(profile):
                # Each profile's task text must be unique: both workers
                # spawn worktrees off the same shared origin repo (git_repo),
                # and each worker's own in-memory Registry numbers its task
                # ids starting at 1 independently, so a shared task string
                # would produce the same "skep/<slug>-1" branch name for
                # both workers and collide in the shared origin repo.
                for _ in range(200):
                    try:
                        await router.cmd_spawn(
                            "g16", profile, repo_name, f"clean {profile}"
                        )
                        return
                    except Exception:
                        await asyncio.sleep(0.02)

            await spawn_when_ready("work")
            await spawn_when_ready("personal")

            for _ in range(300):
                actives = bk.list_active()
                if len(actives) >= 2:
                    break
                await asyncio.sleep(0.02)
            t1.cancel()
            t2.cancel()
    finally:
        await server.close()

    ls = router.format_ls()
    assert "work" in ls
    assert "personal" in ls


async def test_wrong_secret_worker_never_registers():
    server, url, router, bk, gw = await _start_queen(secret="right")
    from pathlib import Path
    wcfg = WorkerConfig(
        host="g16", profile="work", claude_config_dir=None,
        repos_root=Path("/tmp"), worktrees_root=Path("/tmp"),
        db_path=":memory:", queen_url=url, shared_secret="wrong",
    )
    _sup, client = _worker(wcfg, url, secret="wrong")
    try:
        async with aiohttp.ClientSession() as sess:
            task = asyncio.create_task(client.run_once(sess, url))
            await asyncio.sleep(0.3)
            task.cancel()
            # command to the never-registered worker must fail
            with pytest.raises(Exception):
                await router.cmd_spawn("g16", "work", "nix", "task")
    finally:
        await server.close()
