"""Tests for skep.app's single-process worker+router assembly (Plan 1 path)."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, Update, User

from skep.app import build_dispatcher, build_worker_and_router, parse_spawn, watch_roots
from skep.config import QueenConfig, WorkerConfig
from skep.db import Registry
from skep.queen.bookkeeping import Bookkeeping
from skep.queen.mailbox import Mailbox, MailboxService
from skep.supervisor import Supervisor
from skep.transport import SwitchableMailboxClient


def _wcfg(tmp_path, **kw):
    base = dict(
        host="g16", profile="work", claude_config_dir=None,
        repos_root=tmp_path / "repos", worktrees_root=tmp_path / "wt",
        db_path=":memory:", shared_secret="s",
    )
    base.update(kw)
    return WorkerConfig(**base)


class FakeQueenSink:
    """Duck-typed QueenInbox stand-in -- build_worker_and_router never calls
    any of its methods at assembly time, so a bare stub is sufficient."""

    async def on_task_started(
        self, host, profile, local_id, repo, title, session_local_id=None
    ):
        pass

    async def on_activity(self, host, profile, local_id, line):
        pass

    async def on_milestone(self, host, profile, local_id, text):
        pass

    async def on_done(self, host, profile, local_id, status, summary, reset_at=None):
        pass

    async def on_spawn_rejected(self, host, profile, reason):
        pass


def test_build_worker_and_router_activates_mailbox():
    """Merge-blocker regression: the single-process assembly path must also
    give its Supervisor a mailbox_client so spawn() starts a per-agent shim.
    Before this fix, Supervisor(wcfg, registry, worker_sink) was constructed
    with no mailbox_client and the feature was inert here too.
    """
    tmp_path = Path("/tmp")
    bk = Bookkeeping.open(":memory:")
    registry = Registry.open(":memory:")
    router, sup = build_worker_and_router(
        _wcfg(tmp_path), FakeQueenSink(), bk, registry)

    assert isinstance(sup, Supervisor)
    assert isinstance(sup._mailbox_client, SwitchableMailboxClient)  # type: ignore[attr-defined]


async def test_build_worker_and_router_wires_in_process_mailbox(tmp_path):
    """L0.1 #4: given a MailboxService, the single-process assembly must point
    the Supervisor's SwitchableMailboxClient at an in-process target, so an
    agent's send is delivered instead of raising MailboxUnavailable (the
    switch was previously left with no target on this path)."""
    bk = Bookkeeping.open(":memory:")
    registry = Registry.open(":memory:")
    mailbox = Mailbox.open(":memory:")

    delivered = []

    async def deliver_ceo(msg):
        delivered.append(msg)

    async def alert_ceo(text):
        pass

    svc = MailboxService(mailbox, bk, set(), deliver_ceo, alert_ceo)

    wcfg = _wcfg(tmp_path)
    _router, sup = build_worker_and_router(
        wcfg, FakeQueenSink(), bk, registry, mailbox_service=svc)

    # Seed a running agent so tid -> ref resolves (as a real spawn would).
    tid = 1
    bk.add(wcfg.host, wcfg.profile, tid, "repo", "title", 100)

    reply = await sup._mailbox_client.send(  # type: ignore[attr-defined]
        tid, "ceo", "hi", "body", None)
    assert reply.ok and reply.status == "delivered"
    assert len(delivered) == 1


async def test_build_worker_and_router_supervisor_starts_shim_on_spawn(tmp_path):
    """Behavioral proof: spawning through the real assembled Supervisor
    starts a mailbox shim and writes a mailbox entry into the agent's
    --mcp-config file."""
    bk = Bookkeeping.open(":memory:")
    registry = Registry.open(":memory:")
    _router, sup = build_worker_and_router(
        _wcfg(tmp_path, repos_root=tmp_path / "repos",
              worktrees_root=tmp_path / "wt"),
        FakeQueenSink(), bk, registry)
    sup._worktree_factory = lambda *a, **k: None  # type: ignore[attr-defined]

    captured: dict = {}
    shims = []

    class FakeShim:
        def __init__(self, client, tid):
            self.client = client
            self.tid = tid
            self.stopped = False

        async def start(self):
            return f"http://127.0.0.1:9/mcp?tid={self.tid}"

        async def stop(self):
            self.stopped = True

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def start(self):
            pass

        async def events(self):
            if False:
                yield  # pragma: no cover - empty async generator

        async def kill(self):
            pass

        @property
        def pid(self):
            return 1

        @property
        def returncode(self):
            return 0

        @property
        def stderr_text(self):
            return ""

    def shim_factory(client, tid, token=None):
        s = FakeShim(client, tid)
        shims.append(s)
        return s

    sup._agent_factory = lambda **kwargs: FakeAgent(**kwargs)  # type: ignore[attr-defined]
    sup._shim_factory = shim_factory  # type: ignore[attr-defined]

    writes = []
    sup._mcp_config_writer = lambda wt, servers: (  # type: ignore[attr-defined]
        writes.append((wt, servers)) or wt / ".skep" / "mcp.json")

    tid = await sup.spawn("nix", "clean nvidia")

    assert len(shims) == 1
    assert writes[0][1]["mailbox"]["url"] == f"http://127.0.0.1:9/mcp?tid={tid}"
    assert captured["mcp_config_path"].endswith(".skep/mcp.json")
    assert "mcp_servers" not in captured

    pending = list(sup._tasks)  # type: ignore[attr-defined]
    if pending:
        await asyncio.gather(*pending)
    assert shims[0].stopped


def test_parse_spawn_without_watch():
    assert parse_spawn("g16 nix fix the thing") == (
        "g16", "default", "nix", False, "fix the thing",
    )


def test_parse_spawn_with_watch():
    assert parse_spawn("g16 --profile work nix --watch fix the thing") == (
        "g16", "work", "nix", True, "fix the thing",
    )


def test_watch_must_follow_the_repo_not_hide_in_the_task():
    # A --watch that appears later is part of the task text, not a flag.
    host, profile, repo, watch, task = parse_spawn("g16 nix fix --watch the thing")
    assert watch is False
    assert task == "fix --watch the thing"


def test_parse_spawn_rejects_a_watch_with_no_task():
    assert parse_spawn("g16 nix --watch") is None


def test_watch_roots_is_own_worktree_rw_plus_primary_ro():
    assert watch_roots("nix") == [
        {"name": "nix", "mode": "new", "access": "rw"},
        {"name": "nix", "mode": "primary", "access": "ro"},
    ]


# -- /spawn dispatcher wiring -------------------------------------------
#
# No dispatcher/router test fixtures existed anywhere in this repo prior to
# this task (parse_spawn's own tests live in test_integration.py and never
# drive build_dispatcher). These are new, minimal, and only cover the one
# thing this task adds: that --watch changes what roots= the handler passes
# to router.cmd_spawn.

_OWNER_ID = 42


def _qcfg(**overrides):
    kwargs = dict(
        bot_token="123:abc", owner_id=_OWNER_ID, group_chat_id=-100,
        shared_secret="s", bookkeeping_db=":memory:",
    )
    kwargs.update(overrides)
    return QueenConfig(**kwargs)


class _FakeRouter:
    def __init__(self):
        self.cmd_spawn = AsyncMock()


@pytest.fixture
def router():
    return _FakeRouter()


@pytest.fixture
def dispatcher(router):
    return build_dispatcher(router, _qcfg())


async def _send(dispatcher, text):
    """Drive one owner-authored text message through a built Dispatcher."""
    bot = Bot(token="123:abc")
    user = User(id=_OWNER_ID, is_bot=False, first_name="Owner")
    chat = Chat(id=1, type="private")
    message = Message(message_id=1, date=0, chat=chat, from_user=user, text=text)
    update = Update(update_id=1, message=message)
    try:
        with patch.object(Message, "answer", AsyncMock()):
            await dispatcher.feed_update(bot, update)
    finally:
        await bot.session.close()


async def test_spawn_command_with_watch_sends_two_roots(dispatcher, router):
    await _send(dispatcher, "/spawn g16 nix --watch fix it")
    router.cmd_spawn.assert_awaited_once_with(
        "g16",
        "default",
        "nix",
        "fix it",
        roots=[
            {"name": "nix", "mode": "new", "access": "rw"},
            {"name": "nix", "mode": "primary", "access": "ro"},
        ],
    )


async def test_spawn_command_without_watch_sends_no_roots(dispatcher, router):
    await _send(dispatcher, "/spawn g16 nix fix it")
    router.cmd_spawn.assert_awaited_once_with(
        "g16", "default", "nix", "fix it", roots=None
    )
