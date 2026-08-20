from __future__ import annotations

from telegram import Bot
from telegram.constants import ParseMode

from ..digest import split_message


class TelegramChannel:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(self, target: str, message: str) -> None:
        for chunk in split_message(message):
            await self._bot.send_message(
                chat_id=int(target),
                text=chunk,
                parse_mode=ParseMode.HTML,
            )
