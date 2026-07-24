import asyncio

import pytest
from conftest import FakeAgent

from skep.db import Registry
from skep.stream import Event
from skep.supervisor import BASE_TOOLS, Supervisor


@pytest.mark.asyncio
async def test_resume_starts_new_invocation_same_worktree(
    worker_config_no_memory, fake_sink
):
    created = {}

    def fake_agent(**kwargs):
        created.update(kwargs)
        return FakeAgent()

    reg = Registry.open(":memory:")
    first = reg.add_task("nix", "t", "/wt/nix-1")
    reg.update(first, session_local_id=first, resume_token="tok-1", status="done")

    sup = Supervisor(
        worker_config_no_memory, reg, fake_sink,
        agent_factory=fake_agent, worktree_factory=lambda *a: None,
    )
    second = await sup.resume(first, model="claude-opus-4-8")

    task = reg.get_task(second)
    assert second != first
    assert task.session_local_id == first          # same session
    assert task.worktree_path == "/wt/nix-1"        # same worktree, no new one
    assert created["resume_token"] == "tok-1"
    assert created["model"] == "claude-opus-4-8"
    assert created["cwd"].as_posix().endswith("/wt/nix-1")
    # FIX 1: lock the v1-minimal invariant
    assert created["allowed_tools"] == list(BASE_TOOLS)
    assert "mcp_servers" not in created


@pytest.mark.asyncio
async def test_resume_unknown_session_raises(worker_config_no_memory, fake_sink):
    reg = Registry.open(":memory:")
    sup = Supervisor(
        worker_config_no_memory, reg, fake_sink, worktree_factory=lambda *a: None
    )
    with pytest.raises(ValueError):
        await sup.resume(12345)


@pytest.mark.asyncio
async def test_resume_without_resume_token_raises(worker_config_no_memory, fake_sink):
    # FIX 2: cover the no-resume_token error branch
    reg = Registry.open(":memory:")
    first = reg.add_task("nix", "t", "/wt/nix-1")
    reg.update(first, session_local_id=first, status="done")
    # Note: resume_token is None (not set)

    sup = Supervisor(
        worker_config_no_memory, reg, fake_sink, worktree_factory=lambda *a: None,
    )
    with pytest.raises(ValueError):
        await sup.resume(first)


async def _drain(sup: Supervisor) -> None:
    """Await every run_events task the supervisor has in flight."""
    await asyncio.gather(*list(sup._tasks))


def _parked_session(reg: Registry, token: str = "tok-1") -> int:
    sid = reg.add_task("nix", "t", "/wt/nix-1")
    reg.update(sid, session_local_id=sid, resume_token=token, status="parked")
    return sid


class _LiveAgent(FakeAgent):
    """An agent that streams its session id and then keeps running.

    `streaming` is set once run_events has consumed the system event (so the
    invocation row carries a resume_token); `hold` releases the stream.
    """

    def __init__(self):
        super().__init__()
        self.streaming = asyncio.Event()
        self.hold = asyncio.Event()

    async def events(self):
        yield Event(kind="system", session_id="tok-live")
        self.streaming.set()
        await self.hold.wait()


@pytest.mark.asyncio
async def test_second_resume_of_a_live_session_is_rejected(
    worker_config_no_memory, fake_sink
):
    """Two callers (the human and the park sweep) resume one session.

    Once the live invocation has streamed its session id, nothing upstream
    stops a second resume: bookkeeping still says 'parked' until task_started
    round-trips, and latest_invocation now hands back a row WITH a
    resume_token. Without a claim, the second call spawns a second
    `claude --resume` on the same token and the same worktree.
    """
    agents = []

    def factory(**kwargs):
        agent = _LiveAgent()
        agents.append(agent)
        return agent

    reg = Registry.open(":memory:")
    sid = _parked_session(reg)
    sup = Supervisor(
        worker_config_no_memory, reg, fake_sink,
        agent_factory=factory, worktree_factory=lambda *a: None,
    )

    live = await sup.resume(sid)
    await agents[0].streaming.wait()
    assert reg.get_task(live).resume_token == "tok-live"

    with pytest.raises(ValueError, match="live invocation"):
        await sup.resume(sid)

    assert len(agents) == 1  # exactly one agent, not two

    agents[0].hold.set()
    await _drain(sup)


@pytest.mark.asyncio
async def test_failed_resume_releases_the_claim(worker_config_no_memory, fake_sink):
    """A resume that blows up must not wedge its session forever."""
    agents = []

    class BoomAgent(FakeAgent):
        async def start(self):
            raise RuntimeError("agent start failed")

    def factory(**kwargs):
        agents.append(kwargs)
        return BoomAgent() if len(agents) == 1 else FakeAgent()

    reg = Registry.open(":memory:")
    sid = _parked_session(reg)
    sup = Supervisor(
        worker_config_no_memory, reg, fake_sink,
        agent_factory=factory, worktree_factory=lambda *a: None,
    )

    with pytest.raises(RuntimeError):
        await sup.resume(sid)

    retry = await sup.resume(sid)  # claim released -> a retry gets through
    await _drain(sup)
    assert reg.get_task(retry).session_local_id == sid


@pytest.mark.asyncio
async def test_failed_resume_leaves_the_session_resumable(
    worker_config_no_memory, fake_sink
):
    """A resume that dies before its agent streams a session id must not brick
    the session.

    The failed invocation stays the session's `latest_invocation` forever, so a
    row left with resume_token NULL turns every LATER resume into "no
    resume_token to resume from" -- permanently, across worker restarts. The
    sweep retries on its own now, so that is a self-inflicted brick.
    """

    class BoomAgent(FakeAgent):
        async def start(self):
            raise RuntimeError("agent start failed")

    reg = Registry.open(":memory:")
    sid = _parked_session(reg, token="tok-1")
    sup = Supervisor(
        worker_config_no_memory, reg, fake_sink,
        agent_factory=lambda **k: BoomAgent(), worktree_factory=lambda *a: None,
    )

    with pytest.raises(RuntimeError):
        await sup.resume(sid)

    # The dead row is now the session's latest -- it must carry the token it
    # was resuming from, or nothing can ever resume this session again.
    assert reg.latest_invocation(sid).resume_token == "tok-1"


@pytest.mark.asyncio
async def test_a_registry_failure_before_the_try_does_not_leak_the_claim(
    worker_config_no_memory, fake_sink
):
    """The claim is released only by resume's own `except` and by run_events'
    `finally` -- both inside the try. Anything that raises between the claim and
    that try leaks it for the life of the process, permanently blocking the
    session. So the claim must not be taken until the try owns it.
    """
    reg = Registry.open(":memory:")
    sid = _parked_session(reg)
    sup = Supervisor(
        worker_config_no_memory, reg, fake_sink,
        agent_factory=lambda **k: FakeAgent(), worktree_factory=lambda *a: None,
    )

    real_add_task = reg.add_task
    calls = []

    def flaky_add_task(*a, **k):
        calls.append(a)
        if len(calls) == 1:
            raise RuntimeError("sqlite is having a day")
        return real_add_task(*a, **k)

    reg.add_task = flaky_add_task
    with pytest.raises(RuntimeError):
        await sup.resume(sid)

    tid = await sup.resume(sid)  # the claim was never stranded
    await _drain(sup)
    assert reg.get_task(tid).session_local_id == sid


@pytest.mark.asyncio
async def test_successful_resume_records_the_newly_streamed_token(
    worker_config_no_memory, fake_sink
):
    """The seeded token is a floor, never a ceiling: the agent's own `system`
    event must still overwrite it. Without this, seeding could silently mask a
    lost resume_token and every later resume would reuse a stale session."""
    reg = Registry.open(":memory:")
    sid = _parked_session(reg, token="tok-old")
    sup = Supervisor(
        worker_config_no_memory, reg, fake_sink,
        agent_factory=lambda **k: FakeAgent(
            [Event(kind="system", session_id="tok-new")]
        ),
        worktree_factory=lambda *a: None,
    )

    tid = await sup.resume(sid)
    await _drain(sup)

    assert reg.get_task(tid).resume_token == "tok-new"


@pytest.mark.asyncio
async def test_completed_invocation_releases_the_claim(
    worker_config_no_memory, fake_sink
):
    """park -> resume -> park -> resume: a session resumes many times."""
    reg = Registry.open(":memory:")
    sid = _parked_session(reg)
    sup = Supervisor(
        worker_config_no_memory, reg, fake_sink,
        agent_factory=lambda **k: FakeAgent(
            [Event(kind="system", session_id="tok-2")]
        ),
        worktree_factory=lambda *a: None,
    )

    first = await sup.resume(sid)
    await _drain(sup)  # the invocation finishes -> run_events releases it

    second = await sup.resume(sid)
    await _drain(sup)
    assert second != first
    assert reg.get_task(second).session_local_id == sid
