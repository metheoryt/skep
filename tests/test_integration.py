import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
import aiohttp

from conftest import FakeAgent

from skep.app import build_worker_and_router, parse_spawn, watch_roots
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


async def test_watch_spawn_reaches_the_agent_argv(tmp_path, git_repo, fake_claude_cmd):
    """Prove the whole --watch chain end to end: the Telegram-facing
    `router.cmd_spawn` carries `roots` (names only, per spec item 6) into the
    worker's Supervisor, `resolve_roots` maps those names to real paths under
    the worker's own repos_root, and the rendered agent argv shows a fresh
    worktree cwd, the watched checkout as an add_dir, and the READ-ONLY
    declaration.

    Uses two DIFFERENTLY-named roots rather than `app.watch_roots()`'s
    canonical same-name pair. Per `workspace.readonly_declaration`, a
    same-name --watch spawn has both roots resolve to the identical path
    (mode=new's root and mode=primary's root both map to repos_root/<name>),
    so the rw root suppresses its own ro declaration by design -- skep must
    never tell the agent a directory it writes memory into is read-only. That
    collision is exercised directly in test_supervisor.py; pinning it again
    here would only prove the inert case. Two names instead prove the
    declaration itself reaches argv, which is the seam this task is about.
    """
    repo_name = git_repo.name
    watched_name = "watched-checkout"
    (git_repo.parent / watched_name).mkdir()

    wcfg = WorkerConfig(
        host="g16", profile="work", claude_config_dir=None,
        repos_root=git_repo.parent, worktrees_root=tmp_path / "wt",
        db_path=":memory:", max_concurrent=8, claude_bin=fake_claude_cmd,
    )
    gw = _gateway()
    bk = Bookkeeping.open(":memory:")
    router, supervisor = build_worker_and_router(
        wcfg, QueenSink(gw, bk), bk, registry=Registry.open(":memory:")
    )

    calls = []

    def agent_factory(**kwargs):
        calls.append(kwargs)
        return FakeAgent([])

    supervisor._agent_factory = agent_factory  # type: ignore[attr-defined]

    await router.cmd_spawn(
        "g16", "work", repo_name, "look around",
        roots=[
            {"name": repo_name, "mode": "new", "access": "rw"},
            {"name": watched_name, "mode": "primary", "access": "ro"},
        ],
    )

    for _ in range(200):
        entry = bk.by_worker_task("g16", "work", 1)
        if entry and entry.status in ("done", "failed", "killed"):
            break
        await asyncio.sleep(0.02)

    assert len(calls) == 1
    argv = calls[-1]
    assert argv["cwd"].parent == wcfg.worktrees_root              # own fresh worktree
    assert argv["add_dirs"] == [wcfg.repos_root / watched_name]   # watched checkout
    assert "READ-ONLY" in argv["append_system_prompt"]


async def test_watch_roots_canonical_shape_reaches_the_agent_argv(
    tmp_path, git_repo, fake_claude_cmd
):
    """Prove the actual output of `app.watch_roots()` -- the canonical
    same-name pair a real `/spawn <host> <repo> --watch <task>` emits --
    survives the full chain (`router.cmd_spawn` -> Supervisor -> resolve_roots
    -> agent argv), not just the differently-named shape the sibling test
    above uses to keep a READ-ONLY assertion meaningful.

    `watch_roots(repo_name)` is called verbatim (imported from `skep.app`,
    not hand-built) so this test breaks the moment `watch_roots` changes
    shape.
    """
    repo_name = git_repo.name

    wcfg = WorkerConfig(
        host="g16", profile="work", claude_config_dir=None,
        repos_root=git_repo.parent, worktrees_root=tmp_path / "wt",
        db_path=":memory:", max_concurrent=8, claude_bin=fake_claude_cmd,
    )
    gw = _gateway()
    bk = Bookkeeping.open(":memory:")
    router, supervisor = build_worker_and_router(
        wcfg, QueenSink(gw, bk), bk, registry=Registry.open(":memory:")
    )

    calls = []

    def agent_factory(**kwargs):
        calls.append(kwargs)
        return FakeAgent([])

    supervisor._agent_factory = agent_factory  # type: ignore[attr-defined]

    await router.cmd_spawn(
        "g16", "work", repo_name, "look around",
        roots=watch_roots(repo_name),
    )

    for _ in range(200):
        entry = bk.by_worker_task("g16", "work", 1)
        if entry and entry.status in ("done", "failed", "killed"):
            break
        await asyncio.sleep(0.02)

    assert len(calls) == 1
    argv = calls[-1]
    assert argv["cwd"].parent == wcfg.worktrees_root                # own fresh worktree
    assert argv["add_dirs"] == [wcfg.repos_root / repo_name]        # primary checkout

    # Same-name pair: both roots resolve to repos_root/<repo_name>, the
    # identical path. Per `workspace.readonly_declaration` (Task 7), a `ro`
    # root is deliberately suppressed from the declaration when its path is
    # also covered by an `rw` root in the same workspace -- skep must never
    # tell the agent a directory its own memory shim writes into is
    # read-only. So "no READ-ONLY declaration" is the CORRECT outcome here,
    # not a gap: the differently-named sibling test above exists precisely
    # because this same-name case can never exercise the declaration itself.
    assert "READ-ONLY" not in (argv.get("append_system_prompt") or "")


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
