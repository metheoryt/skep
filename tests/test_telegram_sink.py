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
