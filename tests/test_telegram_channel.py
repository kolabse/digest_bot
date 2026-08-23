from datetime import timedelta

import pytest
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter

from digest_bot.channels.telegram import TelegramChannel
from digest_bot.errors import PermanentDeliveryError, RetryableDeliveryError


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send_message(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


async def test_sends_message_as_telegram_html() -> None:
    bot = FakeBot()
    channel = TelegramChannel(bot)

    await channel.send("123", "<b>Дайджест</b>")

    assert bot.calls == [
        {"chat_id": 123, "text": "<b>Дайджест</b>", "parse_mode": ParseMode.HTML}
    ]


class FailingBot:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def send_message(self, **kwargs: object) -> None:
        raise self.error


@pytest.mark.parametrize(
    "error",
    [NetworkError("network"), RetryAfter(timedelta(seconds=15))],
)
async def test_maps_temporary_telegram_errors_to_retryable(error: Exception) -> None:
    channel = TelegramChannel(FailingBot(error))

    with pytest.raises(RetryableDeliveryError):
        await channel.send("123", "Дайджест")


async def test_maps_bad_telegram_request_to_permanent() -> None:
    channel = TelegramChannel(FailingBot(BadRequest("bad target")))

    with pytest.raises(PermanentDeliveryError):
        await channel.send("123", "Дайджест")
