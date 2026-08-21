from datetime import UTC, datetime

from digest_bot.models import Subscription
from digest_bot.service import DeliveryService
from digest_bot.storage import Storage


class FakeSource:
    async def read(self, repository: str, path: str, ref: str, token_env: str | None) -> str:
        return """# Дайджест проекта

## [2026-08-19]

### Доработки

- Дайджест доставлен.
"""


class FakeChannel:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send(self, target: str, message: str) -> None:
        self.messages.append((target, message))


async def test_dispatches_once_for_digest_date(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    storage.add_subscription(
        Subscription(
            id=None,
            channel="telegram",
            target="123",
            repository="owner/repo",
            digest_path="docs/project-digest.md",
            ref="main",
            token_env=None,
            timezone="UTC",
            send_time="08:00",
            created_by=42,
        )
    )
    channel = FakeChannel()
    service = DeliveryService(storage, FakeSource(), {"telegram": channel})
    now = datetime(2026, 8, 20, 8, 30, tzinfo=UTC)

    await service.dispatch_due(now)
    await service.dispatch_due(now)

    assert len(channel.messages) == 1
    target, message = channel.messages[0]
    assert target == "123"
    assert "19.08.2026" in message
    assert "вчера" in message.lower()
    assert "Дайджест доставлен" in message


async def test_preview_works_during_quiet_window(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = storage.add_subscription(
        Subscription(
            id=None,
            channel="telegram",
            target="123",
            repository="owner/repo",
            digest_path="docs/project-digest.md",
            ref="main",
            token_env=None,
            timezone="UTC",
            send_time="08:00",
            created_by=42,
        )
    )
    service = DeliveryService(storage, FakeSource(), {"telegram": FakeChannel()})

    message = await service.preview(
        saved.id,
        "123",
        now=datetime(2026, 8, 20, 16, 0, tzinfo=UTC),
    )

    assert "19.08.2026" in message
    assert "вчера" in message.lower()
    assert "Дайджест доставлен" in message
