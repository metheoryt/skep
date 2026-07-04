from unittest.mock import AsyncMock, MagicMock

import pytest

from skep.queen.onboarding import onboard_group


def _bot(owner_status="member", is_forum=True, can_manage_topics=True):
    bot = MagicMock()
    owner_member = MagicMock(status=owner_status)
    bot_member = MagicMock(status="administrator",
                           can_manage_topics=can_manage_topics)

    async def get_chat_member(chat_id, user_id):
        return owner_member if user_id == 42 else bot_member

    chat = MagicMock(is_forum=is_forum)
    bot.get_chat_member = AsyncMock(side_effect=get_chat_member)
    bot.get_chat = AsyncMock(return_value=chat)
    bot.get_me = AsyncMock(return_value=MagicMock(id=999))
    bot.set_my_commands = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


async def test_onboard_skips_when_owner_absent():
    bot = _bot(owner_status="left")
    assert await onboard_group(bot, chat_id=-100, owner_id=42) == "skipped"
    bot.set_my_commands.assert_not_called()


async def test_onboard_prompts_when_not_forum():
    bot = _bot(is_forum=False)
    assert await onboard_group(bot, chat_id=-100, owner_id=42) == "needs_forum"
    bot.send_message.assert_awaited()  # setup prompt posted


async def test_onboard_ready_registers_commands():
    bot = _bot()
    assert await onboard_group(bot, chat_id=-100, owner_id=42) == "ready"
    bot.set_my_commands.assert_awaited()
