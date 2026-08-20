from datetime import UTC, datetime, timedelta

from digest_bot.models import Subscription
from digest_bot.storage import Storage


def subscription() -> Subscription:
    return Subscription(
        id=None,
        channel="telegram",
        target="123",
        repository="owner/repo",
        digest_path="docs/project-digest.md",
        ref="main",
        token_env=None,
        timezone="UTC",
        send_time="09:00",
        created_by=42,
    )


def test_storage_crud_and_delivery_claim(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = storage.add_subscription(subscription())
    assert saved.id is not None
    assert storage.list_subscriptions(target="123") == [saved]

    now = datetime(2026, 8, 20, tzinfo=UTC)
    assert storage.claim_delivery(saved.id, "2026-08-19", now)
    assert not storage.claim_delivery(saved.id, "2026-08-19", now)
    storage.complete_delivery(saved.id, "2026-08-19", now)
    assert not storage.claim_delivery(saved.id, "2026-08-19", now + timedelta(hours=1))
    assert storage.delete_subscription(saved.id, "123")
    assert storage.list_subscriptions(target="123") == []


def test_stale_delivery_claim_can_be_retried(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = storage.add_subscription(subscription())
    now = datetime(2026, 8, 20, tzinfo=UTC)

    assert storage.claim_delivery(saved.id, "2026-08-19", now)
    assert storage.claim_delivery(saved.id, "2026-08-19", now + timedelta(minutes=11))
