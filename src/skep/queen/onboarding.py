from __future__ import annotations

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat

_COMMANDS = [
    BotCommand(
        command="spawn",
        description="Spawn an agent: <host> [--profile p] <repo> <task>",
    ),
    BotCommand(command="ls", description="List active agents"),
    BotCommand(command="kill", description="Kill an agent by ref"),
    BotCommand(command="panic", description="Kill all agents"),
]

_SETUP_PROMPT = (
    "skep queen is here, but this group isn't ready. Enable Topics "
    "(group settings → Topics) and grant me admin with 'Manage Topics', "
    "then re-add me or re-check."
)


async def is_owner_member(bot: Bot, chat_id: int, owner_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, owner_id)
    return member.status not in ("left", "kicked")


async def onboard_group(bot: Bot, chat_id: int, owner_id: int) -> str:
    if not await is_owner_member(bot, chat_id, owner_id):
        return "skipped"
    chat = await bot.get_chat(chat_id)
    me = await bot.get_me()
    my_member = await bot.get_chat_member(chat_id, me.id)
    can_manage = getattr(my_member, "can_manage_topics", False)
    if not getattr(chat, "is_forum", False) or not can_manage:
        await bot.send_message(chat_id, _SETUP_PROMPT)
        return "needs_forum"
    await bot.set_my_commands(_COMMANDS, scope=BotCommandScopeChat(chat_id=chat_id))
    return "ready"
