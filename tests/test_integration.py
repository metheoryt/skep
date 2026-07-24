import asyncio
import contextlib

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
import aiohttp

from conftest import FakeAgent

from skep.app import build_worker_and_router, parse_spawn, watch_roots
from skep.config import QueenConfig, WorkerConfig
from skep.db import Registry
from skep.queen.assembly import _park_sweep_loop
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.router import QueenRouter
from skep.queen.telegram_sink import QueenSink
from skep.stream import Event
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


async def _start_queen(secret="s", sink_kwargs=None):
    gw = _gateway()
    bk = Bookkeeping.open(":memory:")
    router = QueenRouter(bk)
    sink = QueenSink(gw, bk, **(sink_kwargs or {}))
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


class _BlockingAgent(FakeAgent):
    """An agent whose stream stays open until the test releases it.

    The resumed invocation must still be LIVE when the journal is inspected:
    `rebind_invocation` sets `status='running'`, and `run_events`' finally would
    immediately overwrite it with the terminal status if the event stream ended
    on its own. Releasing the gate lets the invocation finish cleanly so no task
    is left dangling at teardown.
    """

    def __init__(self, session_id="sess-1"):
        super().__init__([])
        self._session_id = session_id
        self.release = asyncio.Event()

    async def events(self):
        await self.release.wait()
        yield Event(
            kind="result", text="finished", is_error=False,
            session_id=self._session_id,
        )


async def _one_sweep_pass(bk, router, now):
    """Run exactly one pass of the queen's park sweep, then cancel it.

    `interval` is huge on purpose: after its single pass the loop parks in
    `asyncio.sleep`, so the cancel below can never race a second pass. The real
    sleep (rather than a bare `sleep(0)`) is what lets the resume frame cross
    the websocket -- on this path `cmd_resume` is fire-and-forget.
    """
    task = asyncio.create_task(
        _park_sweep_loop(bk, router, interval=3600.0, now=lambda: now)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_usage_limit_parks_then_sweep_resumes(tmp_path, git_repo):
    """Sessions A3 end to end, over the real queen<->worker websocket.

    Drives one session through the whole park/auto-resume loop:

    1. `/spawn` -> the agent's terminal `result` is a usage limit;
    2. `run_events` calls it `parked` and carries `reset_at` over the wire, so
       the queen journal parks the row at that instant;
    3. one pass of the queen's park sweep, with `now` past `parked_until`, sends
       a `resume` frame to the (online) worker;
    4. `Supervisor.resume` starts a NEW invocation on the SAME worktree from the
       stored resume_token, and its `task_started` round-trips back into
       `rebind_invocation` -- same ref, same topic, `status='running'`.

    The agent factory is stubbed rather than using `fake_claude` because the
    outcome of each invocation has to be chosen (a usage limit, then a live
    agent that does not finish until the assertions have run); everything
    between the two stubs is production code.
    """
    repo_name = git_repo.name
    reset_at = 1000.0  # POSIX ts; already past by the time the sweep runs
    server, url, router, bk, gw = await _start_queen(
        # No jitter: parked_until must be exactly the reset the worker reported.
        # The sink's clock is frozen at 0 so the forward floor (_MIN_PARK_BACKOFF,
        # which clamps a park deadline that is already in the past) never bites:
        # `reset_at` here is past only relative to the SWEEP's clock, not the
        # sink's, which is the shape a real park has.
        sink_kwargs={"jitter": lambda: 0.0, "now": lambda: 0.0},
    )
    wcfg = WorkerConfig(
        host="g16", profile="work", claude_config_dir=None,
        repos_root=git_repo.parent, worktrees_root=tmp_path / "wt",
        db_path=":memory:", queen_url=url, shared_secret="s",
    )
    sup, client = _worker(wcfg, url)

    limited = FakeAgent([
        Event(kind="system", session_id="sess-1"),
        Event(
            kind="result", text="Claude usage limit reached", is_error=True,
            session_id="sess-1", raw={"subtype": "usage_limit", "reset_at": reset_at},
        ),
    ])
    resumed = _BlockingAgent()
    calls = []

    def agent_factory(**kwargs):
        calls.append(kwargs)
        return limited if len(calls) == 1 else resumed

    sup._agent_factory = agent_factory  # type: ignore[attr-defined]

    try:
        async with aiohttp.ClientSession() as sess:
            conn = asyncio.create_task(client.run_once(sess, url))

            for _ in range(200):
                try:
                    await router.cmd_spawn("g16", "work", repo_name, "clean nvidia")
                    break
                except Exception:
                    await asyncio.sleep(0.02)

            # 1 + 2: the usage limit parks the session at the reported reset.
            for _ in range(200):
                entry = bk.by_worker_task("g16", "work", 1)
                if entry is not None and entry.status == "parked":
                    break
                await asyncio.sleep(0.02)
            parked = bk.by_worker_task("g16", "work", 1)
            assert parked.status == "parked"
            assert parked.parked_until == reset_at
            ref, topic_id = parked.ref, parked.topic_id

            # 3: one sweep pass, now past the deadline, worker online.
            assert router.is_online("g16", "work")
            await _one_sweep_pass(bk, router, now=reset_at + 1000.0)

            # 4: the resume landed and the row is running again on the same ref.
            for _ in range(200):
                entry = bk.get(ref)
                if entry.status == "running":
                    break
                await asyncio.sleep(0.02)
            entry = bk.get(ref)
            assert entry.status == "running"
            assert entry.parked_until is None
            assert entry.topic_id == topic_id     # the topic followed the session
            assert entry.local_id != parked.local_id  # ... onto a new invocation
            assert gw.create_topic.await_count == 1   # and no second topic opened

            # What the worker actually received: a resume of the same worktree
            # off the token the parked invocation left behind.
            assert len(calls) == 2
            assert calls[1]["resume_token"] == "sess-1"
            assert calls[1]["cwd"] == calls[0]["cwd"]

            resumed.release.set()  # let the resumed invocation finish cleanly
            for _ in range(200):
                if bk.get(ref).status == "done":
                    break
                await asyncio.sleep(0.02)
            assert bk.get(ref).status == "done"
            conn.cancel()
    finally:
        await server.close()


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
