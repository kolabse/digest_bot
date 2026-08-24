import logging
from datetime import UTC, datetime, timedelta

from digest_bot.errors import PermanentDeliveryError, RetryableDeliveryError
from digest_bot.models import Subscription
from digest_bot.service import DeliveryService, RetryPolicy
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


class FailingChannel(FakeChannel):
    def __init__(self, failures: list[Exception]) -> None:
        super().__init__()
        self.failures = failures
        self.attempts = 0

    async def send(self, target: str, message: str) -> None:
        self.attempts += 1
        if self.failures:
            raise self.failures.pop(0)
        await super().send(target, message)


def test_retry_policy_uses_exponential_backoff_with_cap() -> None:
    policy = RetryPolicy(initial_delay_seconds=60, max_delay_seconds=300)

    assert [policy.delay_seconds(attempt) for attempt in range(1, 6)] == [
        60,
        120,
        240,
        300,
        300,
    ]


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
    channel = FakeChannel()
    service = DeliveryService(storage, FakeSource(), {"telegram": channel})

    message = await service.preview(
        saved.id,
        "123",
        now=datetime(2026, 8, 20, 16, 0, tzinfo=UTC),
    )

    assert "19.08.2026" in message
    assert "вчера" in message.lower()
    assert "Дайджест доставлен" in message


async def test_preview_matches_delivery_with_custom_texts(tmp_path) -> None:
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
    assert saved.id is not None
    assert storage.add_message_variant(
        saved.id,
        saved.target,
        "intro",
        "Свои новости сегодня:",
        "Свои новости вчера:",
        5,
    ) is not None
    channel = FakeChannel()
    service = DeliveryService(storage, FakeSource(), {"telegram": channel})
    now = datetime(2026, 8, 20, 8, 30, tzinfo=UTC)

    preview = await service.preview(saved.id, saved.target, now=now)
    await service.dispatch_due(now)

    assert len(channel.messages) == 1
    assert channel.messages[0][1] == preview
    assert "Свои новости вчера" in preview


async def test_paused_subscription_is_not_dispatched(tmp_path) -> None:
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
    assert saved.id is not None
    storage.set_subscription_active(saved.id, saved.target, False)
    channel = FakeChannel()
    service = DeliveryService(storage, FakeSource(), {"telegram": channel})

    await service.dispatch_due(datetime(2026, 8, 20, 8, 30, tzinfo=UTC))

    assert channel.messages == []


async def test_retryable_failure_retries_after_persisted_backoff(tmp_path) -> None:
    database_path = tmp_path / "bot.sqlite3"
    storage = Storage(database_path)
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
    assert saved.id is not None
    channel = FailingChannel([RetryableDeliveryError("temporary")])
    policy = RetryPolicy(max_attempts=3, initial_delay_seconds=60, max_delay_seconds=60)
    now = datetime(2026, 8, 20, 8, 30, tzinfo=UTC)
    service = DeliveryService(storage, FakeSource(), {"telegram": channel}, policy)

    await service.dispatch_due(now)
    record = storage.get_delivery(saved.id, "2026-08-19")
    assert record is not None
    assert record.status == "retrying"
    assert record.attempt_count == 1
    assert record.next_attempt_at == (now + timedelta(minutes=1)).isoformat()

    restarted_storage = Storage(database_path)
    restarted_storage.initialize()
    restarted_service = DeliveryService(
        restarted_storage,
        FakeSource(),
        {"telegram": channel},
        policy,
    )
    await restarted_service.dispatch_due(now + timedelta(seconds=59))
    assert channel.attempts == 1
    await restarted_service.dispatch_due(now + timedelta(minutes=1))

    assert channel.attempts == 2
    assert len(channel.messages) == 1
    completed = storage.get_delivery(saved.id, "2026-08-19")
    assert completed is not None
    assert completed.status == "sent"
    assert completed.attempt_count == 2


async def test_provider_retry_delay_overrides_backoff(tmp_path) -> None:
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
    assert saved.id is not None
    channel = FailingChannel(
        [RetryableDeliveryError("limited", retry_after_seconds=180)]
    )
    policy = RetryPolicy(initial_delay_seconds=60, max_delay_seconds=60)
    service = DeliveryService(storage, FakeSource(), {"telegram": channel}, policy)
    now = datetime(2026, 8, 20, 8, 30, tzinfo=UTC)

    await service.dispatch_due(now)

    record = storage.get_delivery(saved.id, "2026-08-19")
    assert record is not None
    assert record.next_attempt_at == (now + timedelta(minutes=3)).isoformat()


async def test_permanent_failure_is_not_retried(tmp_path) -> None:
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
    assert saved.id is not None
    channel = FailingChannel([PermanentDeliveryError("bad target")])
    service = DeliveryService(storage, FakeSource(), {"telegram": channel})
    now = datetime(2026, 8, 20, 8, 30, tzinfo=UTC)

    await service.dispatch_due(now)
    await service.dispatch_due(now + timedelta(minutes=5))

    record = storage.get_delivery(saved.id, "2026-08-19")
    assert record is not None
    assert record.status == "failed"
    assert record.attempt_count == 1
    assert record.error == "PermanentDeliveryError"
    assert channel.attempts == 2
    assert len(channel.messages) == 1
    assert "Рассылка #1 не доставлена" in channel.messages[0][1]
    assert "bad target" not in channel.messages[0][1]


async def test_permanent_notification_failure_is_not_retried_recursively(tmp_path) -> None:
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
    channel = FailingChannel(
        [PermanentDeliveryError("digest"), PermanentDeliveryError("alert")]
    )
    service = DeliveryService(storage, FakeSource(), {"telegram": channel})
    now = datetime(2026, 8, 20, 8, 30, tzinfo=UTC)

    await service.dispatch_due(now)
    await service.dispatch_due(now + timedelta(minutes=5))

    assert saved.id is not None
    assert storage.get_delivery(saved.id, "2026-08-19") is not None
    assert channel.attempts == 2
    assert channel.messages == []


async def test_retryable_failure_stops_at_attempt_limit(tmp_path) -> None:
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
    assert saved.id is not None
    channel = FailingChannel(
        [RetryableDeliveryError("one"), RetryableDeliveryError("two")]
    )
    policy = RetryPolicy(max_attempts=2, initial_delay_seconds=60, max_delay_seconds=60)
    service = DeliveryService(storage, FakeSource(), {"telegram": channel}, policy)
    now = datetime(2026, 8, 20, 8, 30, tzinfo=UTC)

    await service.dispatch_due(now)
    await service.dispatch_due(now + timedelta(minutes=1))
    await service.dispatch_due(now + timedelta(minutes=2))

    record = storage.get_delivery(saved.id, "2026-08-19")
    assert record is not None
    assert record.status == "failed"
    assert record.attempt_count == 2
    assert channel.attempts == 3
    assert len(channel.messages) == 1
    assert "исчерпаны повторные попытки" in channel.messages[0][1]


async def test_failure_notification_retries_without_repeating_digest(tmp_path) -> None:
    database_path = tmp_path / "bot.sqlite3"
    storage = Storage(database_path)
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
    assert saved.id is not None
    channel = FailingChannel(
        [
            PermanentDeliveryError("secret-token"),
            RetryableDeliveryError("notification network"),
        ]
    )
    policy = RetryPolicy(max_attempts=3, initial_delay_seconds=60, max_delay_seconds=60)
    now = datetime(2026, 8, 20, 8, 30, tzinfo=UTC)
    service = DeliveryService(storage, FakeSource(), {"telegram": channel}, policy)

    await service.dispatch_due(now)
    restarted = DeliveryService(
        Storage(database_path),
        FakeSource(),
        {"telegram": channel},
        policy,
    )
    await restarted.dispatch_due(now + timedelta(seconds=59))
    await restarted.dispatch_due(now + timedelta(minutes=1))

    assert channel.attempts == 3
    assert len(channel.messages) == 1
    assert "Рассылка #1 не доставлена" in channel.messages[0][1]
    assert "secret-token" not in channel.messages[0][1]


async def test_stale_last_notification_attempt_is_logged(tmp_path, caplog) -> None:
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
    assert saved.id is not None
    now = datetime(2026, 8, 20, 8, 30, tzinfo=UTC)
    delivery = storage.claim_delivery(saved.id, "2026-08-19", now)
    assert delivery is not None
    assert storage.fail_delivery(
        delivery,
        now,
        "PermanentDeliveryError",
        next_attempt_at=None,
        reason="permanent_error",
    )
    assert storage.claim_failure_notification(
        now,
        max_attempts=1,
        stale_after=timedelta(minutes=1),
    ) is not None
    service = DeliveryService(
        storage,
        FakeSource(),
        {"telegram": FakeChannel()},
        RetryPolicy(max_attempts=1, claim_lease_seconds=60),
    )

    with caplog.at_level(logging.ERROR):
        await service.dispatch_due(now + timedelta(minutes=1))

    assert "Failure notification abandoned after attempt limit" in caplog.text


async def test_notification_backoff_uses_fresh_time_after_slow_failure(tmp_path) -> None:
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
    assert saved.id is not None
    start = datetime(2026, 8, 20, 8, 30, tzinfo=UTC)
    failure_time = start + timedelta(minutes=2)
    delivery = storage.claim_delivery(saved.id, "2026-08-19", start)
    assert delivery is not None
    assert storage.fail_delivery(
        delivery,
        start,
        "PermanentDeliveryError",
        next_attempt_at=None,
        reason="permanent_error",
    )
    times = iter([start, failure_time])
    policy = RetryPolicy(initial_delay_seconds=60, max_delay_seconds=60)
    service = DeliveryService(
        storage,
        FakeSource(),
        {"telegram": FailingChannel([RetryableDeliveryError("slow network")])},
        policy,
        clock=lambda: next(times),
    )

    await service._dispatch_failure_notifications(None)

    assert storage.claim_failure_notification(
        failure_time + timedelta(seconds=59),
        max_attempts=policy.max_attempts,
        stale_after=timedelta(seconds=policy.claim_lease_seconds),
    ) is None
    retry = storage.claim_failure_notification(
        failure_time + timedelta(minutes=1),
        max_attempts=policy.max_attempts,
        stale_after=timedelta(seconds=policy.claim_lease_seconds),
    )
    assert retry is not None
    assert retry.attempt_count == 2


async def test_retry_does_not_cross_delivery_window(tmp_path) -> None:
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
            send_time="13:59",
            created_by=42,
        )
    )
    assert saved.id is not None
    channel = FailingChannel([RetryableDeliveryError("temporary")])
    service = DeliveryService(storage, FakeSource(), {"telegram": channel})

    await service.dispatch_due(datetime(2026, 8, 20, 13, 59, tzinfo=UTC))

    record = storage.get_delivery(saved.id, "2026-08-19")
    assert record is not None
    assert record.status == "failed"
    assert record.attempt_count == 1


async def test_scheduler_expires_retry_after_window_closes(tmp_path) -> None:
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
            send_time="13:55",
            created_by=42,
        )
    )
    assert saved.id is not None
    claim_time = datetime(2026, 8, 20, 13, 55, tzinfo=UTC)
    window_end = datetime(2026, 8, 20, 14, 0, tzinfo=UTC)
    claim = storage.claim_delivery(
        saved.id,
        "2026-08-19",
        claim_time,
        window_end=window_end,
    )
    assert claim is not None
    assert storage.fail_delivery(
        claim,
        claim_time,
        "temporary",
        next_attempt_at=window_end - timedelta(seconds=30),
    )
    channel = FakeChannel()
    service = DeliveryService(storage, FakeSource(), {"telegram": channel})

    await service.dispatch_due(window_end)

    record = storage.get_delivery(saved.id, "2026-08-19")
    assert record is not None
    assert record.status == "failed"
    assert record.failed_at == window_end.isoformat()
    assert len(channel.messages) == 1
    assert "завершилось окно доставки" in channel.messages[0][1]


async def test_failure_backoff_uses_fresh_time_after_slow_operation(tmp_path) -> None:
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
    assert saved.id is not None
    start = datetime(2026, 8, 20, 8, 30, tzinfo=UTC)
    failure_time = start + timedelta(minutes=2)
    times = iter([start, start, failure_time, failure_time])
    channel = FailingChannel([RetryableDeliveryError("temporary")])
    policy = RetryPolicy(initial_delay_seconds=60, max_delay_seconds=60)
    service = DeliveryService(
        storage,
        FakeSource(),
        {"telegram": channel},
        policy,
        clock=lambda: next(times),
    )

    await service.dispatch_due()

    record = storage.get_delivery(saved.id, "2026-08-19")
    assert record is not None
    assert record.attempted_at == start.isoformat()
    assert record.next_attempt_at == (failure_time + timedelta(minutes=1)).isoformat()
