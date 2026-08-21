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


def test_updates_subscription_without_changing_ownership(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = storage.add_subscription(subscription())

    updated = storage.update_subscription(
        Subscription(
            id=saved.id,
            channel="email",
            target=saved.target,
            repository="owner/updated",
            digest_path="docs/new-digest.md",
            ref="develop",
            token_env="GITHUB_TOKEN",
            timezone="Europe/Moscow",
            send_time="22:00",
            created_by=999,
            active=False,
        )
    )

    assert updated is not None
    assert updated.id == saved.id
    assert updated.channel == saved.channel
    assert updated.target == saved.target
    assert updated.created_by == saved.created_by
    assert updated.active == saved.active
    assert updated.repository == "owner/updated"
    assert updated.digest_path == "docs/new-digest.md"
    assert updated.ref == "develop"
    assert updated.token_env == "GITHUB_TOKEN"
    assert updated.timezone == "Europe/Moscow"
    assert updated.send_time == "22:00"


def test_subscription_lookup_and_update_are_scoped_to_target(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = storage.add_subscription(subscription())
    assert saved.id is not None

    assert storage.get_subscription(saved.id, saved.target) == saved
    assert storage.get_subscription(saved.id, "another-chat") is None
    foreign = Subscription(
        id=saved.id,
        channel=saved.channel,
        target="another-chat",
        repository="owner/foreign",
        digest_path=saved.digest_path,
        ref=saved.ref,
        token_env=saved.token_env,
        timezone=saved.timezone,
        send_time=saved.send_time,
        created_by=saved.created_by,
        active=saved.active,
    )
    assert storage.update_subscription(foreign) is None
    assert storage.get_subscription(saved.id, saved.target) == saved
