from __future__ import annotations

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest

from skep.config import QueenConfig


def is_owner(user_id: int | None, owner_id: int) -> bool:
    return user_id is not None and user_id == owner_id


def build_bot(config: QueenConfig) -> Bot:
    return Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2),
    )


class Gateway:
    def __init__(self, bot: Bot, config: QueenConfig):
        self._bot = bot
        self._chat_id = config.group_chat_id

    async def create_topic(self, name: str) -> int:
        topic = await self._bot.create_forum_topic(chat_id=self._chat_id, name=name)
        return topic.message_thread_id

    async def delete_topic(self, topic_id: int) -> None:
        await self._bot.delete_forum_topic(
            chat_id=self._chat_id, message_thread_id=topic_id
        )

    async def post(self, topic_id: int | None, text: str) -> int:
        msg = await self._bot.send_message(
            chat_id=self._chat_id, message_thread_id=topic_id, text=text
        )
        return msg.message_id

    async def edit(self, topic_id: int, message_id: int, text: str) -> None:
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id, message_id=message_id, text=text
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise
