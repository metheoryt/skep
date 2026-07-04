from unittest.mock import AsyncMock, MagicMock

import pytest

from skep.config import QueenConfig
from skep.telegram_gw import Gateway, is_owner


def _cfg():
    return QueenConfig("tok", 42, -1001)


def test_is_owner():
    assert is_owner(42, 42) is True
    assert is_owner(7, 42) is False
    assert is_owner(None, 42) is False


async def test_create_topic_returns_thread_id():
    bot = MagicMock()
    bot.create_forum_topic = AsyncMock(
        return_value=MagicMock(message_thread_id=555)
    )
    gw = Gateway(bot, _cfg())
    tid = await gw.create_topic("nix · task")
    assert tid == 555
    bot.create_forum_topic.assert_awaited_once_with(
        chat_id=-1001, name="nix · task"
    )


async def test_post_returns_message_id():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=9))
    gw = Gateway(bot, _cfg())
    mid = await gw.post(555, "hello")
    assert mid == 9
    bot.send_message.assert_awaited_once_with(
        chat_id=-1001, message_thread_id=555, text="hello"
    )


async def test_edit_swallows_not_modified():
    from aiogram.exceptions import TelegramBadRequest

    bot = MagicMock()
    bot.edit_message_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(),
                                       message="message is not modified")
    )
    gw = Gateway(bot, _cfg())
    # must not raise
    await gw.edit(555, 9, "same text")


async def test_delete_topic_calls_bot():
    bot = MagicMock()
    bot.delete_forum_topic = AsyncMock()
    gw = Gateway(bot, _cfg())
    await gw.delete_topic(555)
    bot.delete_forum_topic.assert_awaited_once_with(
        chat_id=-1001, message_thread_id=555
    )
