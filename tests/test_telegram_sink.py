import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from skep.queen.bookkeeping import Bookkeeping
from skep.queen.telegram_sink import QueenSink


def _gateway():
    gw = MagicMock()
    gw.create_topic = AsyncMock(return_value=555)
    gw.post = AsyncMock(return_value=9)
    gw.edit = AsyncMock()
    return gw


async def test_spawn_rejected_renders_spawn_verb_by_default():
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)
    await sink.on_spawn_rejected("g16", "work", "at capacity")
    gw.post.assert_awaited_once_with(None, "spawn on g16/work rejected: at capacity")


async def test_spawn_rejected_renders_resume_verb_when_specified():
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)
    await sink.on_spawn_rejected(
        "g16", "work", "no such session: 7", action="resume"
    )
    gw.post.assert_awaited_once_with(
        None, "resume on g16/work rejected: no such session: 7"
    )


async def test_sweep_origin_rejection_does_not_notify_the_owner():
    """A worker at capacity rejects every sweep tick's resume -- one owner
    notification every park_sweep_interval for the whole busy period. Nothing
    on the rejection path clears `parked`, so the same entry comes due again
    next tick. Routine, machine-driven, and it must stay off Telegram."""
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)
    await sink.on_spawn_rejected(
        "g16", "work", "at capacity", "resume", "sweep"
    )
    gw.post.assert_not_awaited()


async def test_manual_resume_rejection_still_notifies_the_owner():
    """/resume answers optimistically ("Resuming ref N") and the real failure
    only arrives later as this frame -- suppressing it would leave the human
    believing a resume that never happened."""
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)
    await sink.on_spawn_rejected("g16", "work", "at capacity", "resume")
    gw.post.assert_awaited_once_with(
        None, "resume on g16/work rejected: at capacity"
    )


async def test_task_started_creates_topic_and_entry():
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)
    await sink.on_task_started("g16", "work", 5, "nix", "clean nvidia")
    gw.create_topic.assert_awaited_once_with("g16·work·nix")
    e = bk.by_worker_task("g16", "work", 5)
    assert e.topic_id == 555


async def test_activity_posts_then_edits_and_escapes():
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)
    await sink.on_task_started("g16", "work", 5, "nix", "t")

    await sink.on_activity("g16", "work", 5, "🔧 edit_file")
    gw.post.assert_awaited_once_with(555, r"🔧 edit\_file")   # escaped on the queen
    ref = bk.by_worker_task("g16", "work", 5).ref
    assert bk.get(ref).activity_msg_id == 9

    await sink.on_activity("g16", "work", 5, "💬 v1.2")
    gw.edit.assert_awaited_once_with(555, 9, r"💬 v1\.2")


async def test_milestone_posts_escaped():
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)
    await sink.on_task_started("g16", "work", 5, "nix", "t")
    await sink.on_milestone("g16", "work", 5, "✅ Done: v1.2-3")
    gw.post.assert_awaited_with(555, r"✅ Done: v1\.2\-3")


async def test_done_sets_status():
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)
    await sink.on_task_started("g16", "work", 5, "nix", "t")
    await sink.on_done("g16", "work", 5, "done", "finished")
    ref = bk.by_worker_task("g16", "work", 5).ref
    assert bk.get(ref).status == "done"


async def test_activity_for_unknown_task_is_ignored():
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)
    # no on_task_started first — must not raise
    await sink.on_activity("g16", "work", 99, "orphan")
    gw.post.assert_not_awaited()


async def test_on_task_started_is_reattach_idempotent():
    from unittest.mock import AsyncMock, MagicMock
    from skep.queen.bookkeeping import Bookkeeping
    from skep.queen.telegram_sink import QueenSink

    gw = MagicMock()
    gw.create_topic = AsyncMock(return_value=100)
    gw.post = AsyncMock(return_value=1)
    bk = Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)

    await sink.on_task_started("g16", "work", 1, "nix", "clean")
    await sink.on_task_started("g16", "work", 1, "nix", "clean")  # re-register

    assert gw.create_topic.await_count == 1  # no duplicate topic
    assert bk.by_worker_task("g16", "work", 1) is not None


async def test_second_invocation_reuses_ref_and_topic():
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)

    await sink.on_task_started("g16", "work", 5, "nix", "t", session_local_id=5)
    ref = bk.by_worker_task("g16", "work", 5).ref
    await sink.on_done("g16", "work", 5, "done", "")

    # A resume: new invocation id, same session.
    await sink.on_task_started("g16", "work", 9, "nix", "t", session_local_id=5)

    gw.create_topic.assert_awaited_once()          # NOT a second topic
    e = bk.by_worker_task("g16", "work", 9)
    assert e.ref == ref
    assert e.topic_id == 555
    assert e.status == "running"


async def test_unknown_session_creates_a_new_topic():
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)
    await sink.on_task_started("g16", "work", 5, "nix", "t", session_local_id=5)
    await sink.on_task_started("g16", "work", 9, "nix", "t", session_local_id=9)
    assert gw.create_topic.await_count == 2


async def test_task_started_without_session_id_behaves_as_before():
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)
    await sink.on_task_started("g16", "work", 5, "nix", "t")
    gw.create_topic.assert_awaited_once_with("g16·work·nix")
    assert bk.by_worker_task("g16", "work", 5).session_local_id == 5


async def test_reattach_of_the_same_invocation_is_still_idempotent():
    gw, bk = _gateway(), Bookkeeping.open(":memory:")
    sink = QueenSink(gw, bk)
    await sink.on_task_started("g16", "work", 5, "nix", "t", session_local_id=5)
    await sink.on_task_started("g16", "work", 5, "nix", "t", session_local_id=5)
    gw.create_topic.assert_awaited_once()


def _sink_with_entry():
    bk = Bookkeeping.open(":memory:")
    ref = bk.add("h", "p", 1, "repo", "task", topic_id=100)
    gw = _FakeGateway()
    sink = QueenSink(gw, bk, park_default_backoff=3600.0,
                     now=lambda: 1000.0, jitter=lambda: 0.0)
    return bk, ref, gw, sink


class _FakeGateway:
    def __init__(self):
        self.posts = []

    async def create_topic(self, name):
        return 100

    async def post(self, topic_id, text):
        self.posts.append((topic_id, text))
        return 1

    async def edit(self, *a, **k): ...


def test_parked_done_parks_with_known_reset():
    bk, ref, gw, sink = _sink_with_entry()
    asyncio.run(sink.on_done("h", "p", 1, "parked", "limit", reset_at=5000.0))
    e = bk.get(ref)
    assert e.status == "parked"
    assert e.parked_until == 5000.0
    assert gw.posts  # a "resumes ~..." notice was posted to the topic


def test_parked_done_uses_backoff_when_reset_unknown():
    bk, ref, gw, sink = _sink_with_entry()
    asyncio.run(sink.on_done("h", "p", 1, "parked", "limit", reset_at=None))
    e = bk.get(ref)
    assert e.parked_until == 1000.0 + 3600.0  # now + backoff, jitter=0


def test_parked_done_keeps_mailbox():
    bk, ref, gw, sink = _sink_with_entry()

    class _MB:
        def __init__(self): self.gone = []
        async def handle_recipient_gone(self, ref): self.gone.append(ref)

    mb = _MB()
    sink._mailbox_service = mb
    asyncio.run(sink.on_done("h", "p", 1, "parked", "limit", reset_at=5000.0))
    assert mb.gone == []  # parked session's mailbox is NOT torn down


def test_ordinary_done_still_sets_status_and_clears_mailbox():
    bk, ref, gw, sink = _sink_with_entry()

    class _MB:
        def __init__(self): self.gone = []
        async def handle_recipient_gone(self, ref): self.gone.append(ref)

    mb = _MB()
    sink._mailbox_service = mb
    asyncio.run(sink.on_done("h", "p", 1, "done", "ok", reset_at=None))
    assert bk.get(ref).status == "done"
    assert mb.gone == [ref]
