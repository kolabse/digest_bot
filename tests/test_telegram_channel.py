from telegram.constants import ParseMode

from digest_bot.channels.telegram import TelegramChannel


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
