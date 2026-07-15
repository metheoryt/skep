"""Tests for skep.app's single-process worker+router assembly (Plan 1 path)."""

import asyncio
from pathlib import Path

from skep.app import build_worker_and_router
from skep.config import WorkerConfig
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

    async def on_task_started(self, host, profile, local_id, repo, title):
        pass

    async def on_activity(self, host, profile, local_id, line):
        pass

    async def on_milestone(self, host, profile, local_id, text):
        pass

    async def on_done(self, host, profile, local_id, status, summary):
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
