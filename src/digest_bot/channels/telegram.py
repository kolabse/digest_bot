from __future__ import annotations

import math

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, RetryAfter

from ..digest import split_message
from ..errors import PermanentDeliveryError, RetryableDeliveryError


class TelegramChannel:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(
        self,
        target: str,
        message: str,
        *,
        delivery_key: str | None = None,
    ) -> None:
        try:
            for chunk in split_message(message):
                await self._bot.send_message(
                    chat_id=int(target),
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                )
        except RetryAfter as exc:
            retry_after = exc.retry_after
            seconds = (
                retry_after.total_seconds()
                if hasattr(retry_after, "total_seconds")
                else float(retry_after)
            )
            raise RetryableDeliveryError(
                "Telegram временно ограничил частоту отправки.",
                retry_after_seconds=seconds if math.isfinite(seconds) and seconds > 0 else None,
            ) from exc
        except (BadRequest, Forbidden, InvalidToken) as exc:
            raise PermanentDeliveryError(f"Telegram отклонил сообщение: {exc}") from exc
        except NetworkError as exc:
            raise RetryableDeliveryError("Ошибка сети Telegram.") from exc
