import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

import digest_bot.migrations as migrations_module
from digest_bot.migrations import (
    LATEST_SCHEMA_VERSION,
    SchemaMigrationError,
    UnsupportedSchemaVersionError,
)
from digest_bot.models import Subscription
from digest_bot.storage import StaleMessageVariantsError, Storage

LEGACY_SCHEMA = """
CREATE TABLE subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    target TEXT NOT NULL,
    repository TEXT NOT NULL,
    digest_path TEXT NOT NULL,
    ref TEXT NOT NULL,
    token_env TEXT,
    timezone TEXT NOT NULL,
    send_time TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE deliveries (
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    digest_date TEXT NOT NULL,
    status TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    sent_at TEXT,
    error TEXT,
    PRIMARY KEY (subscription_id, digest_date)
);
"""


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


def test_initialization_versions_legacy_database_without_losing_data(tmp_path) -> None:
    database_path = tmp_path / "bot.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(LEGACY_SCHEMA)
        connection.execute(
            """
            INSERT INTO subscriptions (
                channel, target, repository, digest_path, ref, token_env,
                timezone, send_time, created_by, active
            ) VALUES ('telegram', '123', 'owner/repo', 'docs/project-digest.md',
                      'main', 'GITHUB_TOKEN', 'UTC', '09:00', 42, 1)
            """
        )
        connection.execute(
            """
            INSERT INTO deliveries (
                subscription_id, digest_date, status, attempted_at, sent_at, error
            ) VALUES (1, '2026-08-19', 'sent', '2026-08-20T08:30:00+00:00',
                      '2026-08-20T08:30:01+00:00', NULL)
            """
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0

    storage = Storage(database_path)
    storage.initialize()

    assert storage.list_subscriptions(target="123") == [
        Subscription(
            id=1,
            channel="telegram",
            target="123",
            repository="owner/repo",
            digest_path="docs/project-digest.md",
            ref="main",
            token_env="GITHUB_TOKEN",
            timezone="UTC",
            send_time="09:00",
            created_by=42,
        )
    ]
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            LATEST_SCHEMA_VERSION
        )
        assert connection.execute(
            """
            SELECT status, error, attempt_count, next_attempt_at, claim_token
            FROM deliveries WHERE subscription_id = 1
            """
        ).fetchone() == ("sent", None, 1, None, None)
    assert not storage.claim_delivery(
        1,
        "2026-08-19",
        datetime(2026, 8, 20, 9, tzinfo=UTC),
    )


def test_initialization_rejects_unknown_unversioned_schema(tmp_path) -> None:
    database_path = tmp_path / "bot.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE subscriptions (
                id INTEGER,
                channel TEXT,
                target TEXT,
                repository TEXT,
                digest_path TEXT,
                ref TEXT,
                token_env TEXT,
                timezone TEXT,
                send_time TEXT,
                created_by INTEGER,
                active INTEGER,
                created_at TEXT
            );
            CREATE TABLE deliveries (
                subscription_id INTEGER,
                digest_date TEXT,
                status TEXT,
                attempted_at TEXT,
                sent_at TEXT,
                error TEXT
            );
            """
        )

    with pytest.raises(SchemaMigrationError, match="v0.1.0 schema"):
        Storage(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0


def test_initialization_rejects_legacy_schema_without_autoincrement(tmp_path) -> None:
    database_path = tmp_path / "bot.sqlite3"
    altered_schema = LEGACY_SCHEMA.replace(
        "id INTEGER PRIMARY KEY AUTOINCREMENT",
        "id INTEGER PRIMARY KEY",
    )
    with sqlite3.connect(database_path) as connection:
        connection.executescript(altered_schema)

    with pytest.raises(SchemaMigrationError, match="v0.1.0 schema"):
        Storage(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0


def test_initialization_rejects_unknown_view_in_unversioned_database(tmp_path) -> None:
    database_path = tmp_path / "bot.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE VIEW unknown_view AS SELECT 1 AS value")

    with pytest.raises(SchemaMigrationError, match="v0.1.0 schema"):
        Storage(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT value FROM unknown_view").fetchone()[0] == 1


def test_initialization_validates_current_schema(tmp_path) -> None:
    database_path = tmp_path / "bot.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE subscriptions (id INTEGER PRIMARY KEY)")
        connection.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION}")

    with pytest.raises(
        SchemaMigrationError,
        match=rf"version {LATEST_SCHEMA_VERSION} schema",
    ):
        Storage(database_path).initialize()


def test_initialization_creates_current_schema_and_is_idempotent(tmp_path) -> None:
    database_path = tmp_path / "bot.sqlite3"
    storage = Storage(database_path)

    storage.initialize()
    saved = storage.add_subscription(subscription())
    storage.initialize()

    assert storage.list_subscriptions() == [saved]
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            LATEST_SCHEMA_VERSION
        )
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        assert {
            "subscriptions",
            "deliveries",
            "delivery_failure_notifications",
            "message_variants",
        } <= tables


def test_message_variant_crud_is_scoped_to_subscription_chat(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = storage.add_subscription(subscription())
    assert saved.id is not None

    variant = storage.add_message_variant(
        saved.id,
        "123",
        "intro",
        "Новости за сегодня:",
        "Новости за вчера:",
        5,
    )

    assert variant is not None
    assert variant.id is not None
    assert storage.list_message_variants(saved.id, target="123") == [variant]
    assert storage.list_message_variants(saved.id, target="999") is None
    assert not storage.delete_message_variant(saved.id, "999", variant.id, "intro")
    fallback = storage.add_message_variant(
        saved.id,
        "123",
        "fallback",
        "Сегодня тихо",
        "Вчера было тихо",
        1,
    )
    assert fallback is not None
    assert fallback.id is not None
    assert not storage.delete_message_variant(saved.id, "123", fallback.id, "intro")
    assert storage.clear_message_variants(saved.id, "123", kind="intro") == 1
    assert storage.list_message_variants(saved.id, target="123") == [fallback]


def test_replace_message_variants_is_atomic_and_scoped(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = storage.add_subscription(subscription())
    assert saved.id is not None

    replaced = storage.replace_message_variants(
        saved.id,
        "123",
        [
            ("intro", "Сегодня", "Вчера", 5),
            ("fallback", "Тихо сегодня", "Тихо вчера", 1),
        ],
    )

    assert replaced is not None
    assert [item.kind for item in replaced] == ["fallback", "intro"]
    assert storage.replace_message_variants(saved.id, "999", []) is None
    assert storage.list_message_variants(saved.id, target="123") == replaced


def test_replace_message_variants_rejects_stale_draft(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = storage.add_subscription(subscription())
    assert saved.id is not None
    original = storage.list_message_variants(saved.id, target="123")
    assert original == []
    revision = storage.message_variants_revision(original)

    storage.replace_message_variants(
        saved.id,
        "123",
        [("intro", "Первое изменение", "Первое изменение", 1)],
        expected_revision=revision,
    )

    with pytest.raises(StaleMessageVariantsError):
        storage.replace_message_variants(
            saved.id,
            "123",
            [("intro", "Второе изменение", "Второе изменение", 1)],
            expected_revision=revision,
        )


def test_message_variant_validation_and_limit(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = storage.add_subscription(subscription())
    assert saved.id is not None

    with pytest.raises(ValueError, match="1 to 500"):
        storage.add_message_variant(saved.id, "123", "intro", "", "yesterday", 1)
    with pytest.raises(ValueError, match="between 1 and 100"):
        storage.add_message_variant(saved.id, "123", "intro", "today", "yesterday", 0)
    for index in range(20):
        assert storage.add_message_variant(
            saved.id,
            "123",
            "intro",
            f"today {index}",
            f"yesterday {index}",
            1,
        ) is not None
    with pytest.raises(ValueError, match="at most 20"):
        storage.add_message_variant(
            saved.id,
            "123",
            "intro",
            "one too many",
            "one too many",
            1,
        )


def test_version_2_migration_preserves_deliveries_and_adds_empty_outbox(tmp_path) -> None:
    database_path = tmp_path / "bot.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(migrations_module.SUBSCRIPTIONS_SCHEMA)
        connection.execute(migrations_module.DELIVERIES_SCHEMA)
        connection.execute(migrations_module.DELIVERIES_RETRY_INDEX)
        connection.execute(
            """
            INSERT INTO subscriptions (
                channel, target, repository, digest_path, ref, token_env,
                timezone, send_time, created_by, active
            ) VALUES ('telegram', '123', 'owner/repo', 'docs/project-digest.md',
                      'main', NULL, 'UTC', '09:00', 42, 1)
            """
        )
        connection.execute(
            """
            INSERT INTO deliveries (
                subscription_id, digest_date, status, attempted_at, sent_at,
                error, attempt_count, next_attempt_at, failed_at, claim_token,
                window_end_at, lease_expires_at
            ) VALUES (1, '2026-08-19', 'sent', '2026-08-20T09:00:00+00:00',
                      '2026-08-20T09:00:01+00:00', NULL, 1, NULL, NULL, NULL,
                      '2026-08-20T14:00:00+00:00', NULL)
            """
        )
        connection.execute("PRAGMA user_version = 2")

    storage = Storage(database_path)
    storage.initialize()

    record = storage.get_delivery(1, "2026-08-19")
    assert record is not None
    assert record.status == "sent"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            LATEST_SCHEMA_VERSION
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM delivery_failure_notifications"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM message_variants").fetchone()[0] == 0


def test_delivery_stats_are_scoped_and_include_latest_record(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    first = storage.add_subscription(subscription())
    second = storage.add_subscription(replace(subscription(), target="999"))
    assert first.id is not None
    assert second.id is not None
    now = datetime(2026, 8, 20, 9, tzinfo=UTC)
    first_claim = storage.claim_delivery(first.id, "2026-08-19", now)
    second_claim = storage.claim_delivery(second.id, "2026-08-19", now)
    assert first_claim is not None
    assert second_claim is not None
    assert storage.complete_delivery(first_claim, now)
    assert storage.fail_delivery(
        second_claim,
        now,
        "PermanentDeliveryError",
        next_attempt_at=None,
        reason="permanent_error",
    )

    stats = storage.delivery_stats("123", first.id)

    assert stats is not None
    assert (stats.total, stats.sent, stats.failed, stats.attempts) == (1, 1, 0, 1)
    assert stats.latest is not None
    assert stats.latest.digest_date == "2026-08-19"
    assert storage.delivery_stats("123", second.id) is None


def test_failure_notification_is_durable_and_cleanup_waits_for_it(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = storage.add_subscription(subscription())
    assert saved.id is not None
    now = datetime(2026, 8, 20, 9, tzinfo=UTC)
    claim = storage.claim_delivery(saved.id, "2026-08-19", now)
    assert claim is not None
    assert storage.fail_delivery(
        claim,
        now,
        "PermanentDeliveryError: secret-value",
        next_attempt_at=None,
        reason="permanent_error",
    )

    notification = storage.claim_failure_notification(
        now,
        max_attempts=3,
        stale_after=timedelta(minutes=10),
    )
    assert notification is not None
    assert notification.reason == "permanent_error"
    assert notification.target == "123"
    assert storage.cleanup_deliveries(now + timedelta(days=1), batch_size=10) == 0
    assert storage.complete_failure_notification(notification, now)
    assert storage.cleanup_deliveries(now + timedelta(days=1), batch_size=10) == 1
    assert storage.get_delivery(saved.id, "2026-08-19") is None


def test_failure_notification_retry_survives_restart(tmp_path) -> None:
    database_path = tmp_path / "bot.sqlite3"
    storage = Storage(database_path)
    storage.initialize()
    saved = storage.add_subscription(subscription())
    assert saved.id is not None
    now = datetime(2026, 8, 20, 9, tzinfo=UTC)
    delivery = storage.claim_delivery(saved.id, "2026-08-19", now)
    assert delivery is not None
    assert storage.fail_delivery(
        delivery,
        now,
        "RetryableDeliveryError",
        next_attempt_at=None,
        reason="attempts_exhausted",
    )
    first = storage.claim_failure_notification(
        now,
        max_attempts=3,
        stale_after=timedelta(minutes=10),
    )
    assert first is not None
    retry_at = now + timedelta(minutes=1)
    assert storage.fail_failure_notification(
        first,
        now,
        "NetworkError",
        next_attempt_at=retry_at,
    )

    restarted = Storage(database_path)
    restarted.initialize()
    assert restarted.claim_failure_notification(
        retry_at - timedelta(seconds=1),
        max_attempts=3,
        stale_after=timedelta(minutes=10),
    ) is None
    second = restarted.claim_failure_notification(
        retry_at,
        max_attempts=3,
        stale_after=timedelta(minutes=10),
    )
    assert second is not None
    assert second.attempt_count == 2


def test_failure_notification_stale_claim_is_token_fenced(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = storage.add_subscription(subscription())
    assert saved.id is not None
    now = datetime(2026, 8, 20, 9, tzinfo=UTC)
    delivery = storage.claim_delivery(saved.id, "2026-08-19", now)
    assert delivery is not None
    assert storage.fail_delivery(
        delivery,
        now,
        "PermanentDeliveryError",
        next_attempt_at=None,
        reason="permanent_error",
    )
    first = storage.claim_failure_notification(
        now,
        max_attempts=3,
        stale_after=timedelta(minutes=1),
    )
    assert first is not None
    second = storage.claim_failure_notification(
        now + timedelta(minutes=1),
        max_attempts=3,
        stale_after=timedelta(minutes=1),
    )
    assert second is not None
    assert second.attempt_count == 2
    assert not storage.complete_failure_notification(first, now + timedelta(minutes=1))
    assert storage.complete_failure_notification(second, now + timedelta(minutes=1))


def test_exhausted_stale_notification_is_reported_as_abandoned(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = storage.add_subscription(subscription())
    assert saved.id is not None
    now = datetime(2026, 8, 20, 9, tzinfo=UTC)
    delivery = storage.claim_delivery(saved.id, "2026-08-19", now)
    assert delivery is not None
    assert storage.fail_delivery(
        delivery,
        now,
        "PermanentDeliveryError",
        next_attempt_at=None,
        reason="permanent_error",
    )
    notification = storage.claim_failure_notification(
        now,
        max_attempts=1,
        stale_after=timedelta(minutes=1),
    )
    assert notification is not None

    abandoned = storage.abandon_exhausted_failure_notifications(
        now + timedelta(minutes=1),
        max_attempts=1,
    )

    assert abandoned == [(saved.id, "2026-08-19", "telegram")]
    assert storage.claim_failure_notification(
        now + timedelta(minutes=1),
        max_attempts=1,
        stale_after=timedelta(minutes=1),
    ) is None
    assert not storage.complete_failure_notification(
        notification,
        now + timedelta(minutes=1),
    )


def test_cleanup_keeps_open_recent_and_cutoff_boundary_deliveries(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = storage.add_subscription(subscription())
    assert saved.id is not None
    cutoff = datetime(2026, 8, 20, 9, tzinfo=UTC)

    old = storage.claim_delivery(saved.id, "2026-08-16", cutoff - timedelta(days=2))
    boundary = storage.claim_delivery(saved.id, "2026-08-17", cutoff)
    recent = storage.claim_delivery(saved.id, "2026-08-18", cutoff + timedelta(seconds=1))
    open_claim = storage.claim_delivery(
        saved.id,
        "2026-08-19",
        cutoff - timedelta(days=3),
        window_end=cutoff + timedelta(days=1),
    )
    assert old is not None
    assert boundary is not None
    assert recent is not None
    assert open_claim is not None
    assert storage.complete_delivery(old, cutoff - timedelta(days=2))
    assert storage.complete_delivery(boundary, cutoff)
    assert storage.complete_delivery(recent, cutoff + timedelta(seconds=1))

    assert storage.cleanup_deliveries(cutoff, batch_size=10) == 1
    assert storage.get_delivery(saved.id, "2026-08-16") is None
    assert storage.get_delivery(saved.id, "2026-08-17") is not None
    assert storage.get_delivery(saved.id, "2026-08-18") is not None
    assert storage.get_delivery(saved.id, "2026-08-19") is not None


def test_initialization_rejects_newer_schema_version(tmp_path) -> None:
    database_path = tmp_path / "bot.sqlite3"
    future_version = LATEST_SCHEMA_VERSION + 1
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE future_data (value TEXT)")
        connection.execute("INSERT INTO future_data VALUES ('preserve me')")
        connection.execute(f"PRAGMA user_version = {future_version}")

    with pytest.raises(UnsupportedSchemaVersionError, match="newer than supported"):
        Storage(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == future_version
        assert connection.execute("SELECT value FROM future_data").fetchone()[0] == (
            "preserve me"
        )


def test_failed_migration_is_rolled_back(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "bot.sqlite3"

    def broken_migration(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE partial_change (value TEXT)")
        connection.execute("INSERT INTO partial_change VALUES ('must roll back')")
        raise RuntimeError("migration failed")

    monkeypatch.setattr(migrations_module, "MIGRATIONS", {1: broken_migration})
    monkeypatch.setattr(migrations_module, "LATEST_SCHEMA_VERSION", 1)

    with pytest.raises(RuntimeError, match="migration failed"):
        Storage(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE name = 'partial_change'"
        ).fetchone() is None


def test_storage_crud_and_delivery_claim(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = storage.add_subscription(subscription())
    assert saved.id is not None
    assert storage.list_subscriptions(target="123") == [saved]

    now = datetime(2026, 8, 20, tzinfo=UTC)
    claim = storage.claim_delivery(saved.id, "2026-08-19", now)
    assert claim is not None
    assert not storage.claim_delivery(saved.id, "2026-08-19", now)
    assert storage.complete_delivery(claim, now)
    assert not storage.claim_delivery(saved.id, "2026-08-19", now + timedelta(hours=1))
    assert storage.delete_subscription(saved.id, "123")
    assert storage.list_subscriptions(target="123") == []


def test_stale_delivery_claim_can_be_retried(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = storage.add_subscription(subscription())
    now = datetime(2026, 8, 20, tzinfo=UTC)

    first_claim = storage.claim_delivery(saved.id, "2026-08-19", now)
    assert first_claim is not None
    second_claim = storage.claim_delivery(
        saved.id,
        "2026-08-19",
        now + timedelta(minutes=11),
    )
    assert second_claim is not None
    assert second_claim.attempt_count == 2
    assert second_claim.token != first_claim.token
    assert not storage.complete_delivery(first_claim, now + timedelta(minutes=11))


def test_initialization_recovers_legacy_in_progress_delivery(tmp_path) -> None:
    database_path = tmp_path / "bot.sqlite3"
    attempted_at = "2026-08-20T08:30:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(LEGACY_SCHEMA)
        connection.execute(
            """
            INSERT INTO subscriptions (
                channel, target, repository, digest_path, ref, token_env,
                timezone, send_time, created_by, active
            ) VALUES ('telegram', '123', 'owner/repo', 'docs/project-digest.md',
                      'main', NULL, 'UTC', '09:00', 42, 1)
            """
        )
        connection.execute(
            """
            INSERT INTO deliveries (
                subscription_id, digest_date, status, attempted_at
            ) VALUES (1, '2026-08-19', 'sending', ?)
            """,
            (attempted_at,),
        )
        connection.execute("PRAGMA user_version = 1")

    storage = Storage(database_path)
    storage.initialize()

    record = storage.get_delivery(1, "2026-08-19")
    assert record is not None
    assert record.status == "failed"
    assert record.attempt_count == 1
    assert record.next_attempt_at is None
    assert record.failed_at == attempted_at
    assert record.claim_token is None
    assert "Ambiguous in-progress" in (record.error or "")


def test_initialization_rejects_unknown_legacy_delivery_status(tmp_path) -> None:
    database_path = tmp_path / "bot.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(LEGACY_SCHEMA)
        connection.execute(
            """
            INSERT INTO subscriptions (
                channel, target, repository, digest_path, ref, token_env,
                timezone, send_time, created_by, active
            ) VALUES ('telegram', '123', 'owner/repo', 'docs/project-digest.md',
                      'main', NULL, 'UTC', '09:00', 42, 1)
            """
        )
        connection.execute(
            """
            INSERT INTO deliveries (
                subscription_id, digest_date, status, attempted_at
            ) VALUES (1, '2026-08-19', 'unknown', '2026-08-20T08:30:00+00:00')
            """
        )
        connection.execute("PRAGMA user_version = 1")

    with pytest.raises(SchemaMigrationError, match="invalid states"):
        Storage(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT status FROM deliveries").fetchone()[0] == (
            "unknown"
        )


def test_failed_delivery_waits_for_backoff_and_becomes_terminal(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = storage.add_subscription(subscription())
    assert saved.id is not None
    now = datetime(2026, 8, 20, tzinfo=UTC)
    first_claim = storage.claim_delivery(saved.id, "2026-08-19", now, max_attempts=2)
    assert first_claim is not None
    retry_at = now + timedelta(minutes=1)

    assert storage.fail_delivery(
        first_claim,
        now,
        "temporary error",
        next_attempt_at=retry_at,
    )
    assert storage.claim_delivery(
        saved.id,
        "2026-08-19",
        retry_at - timedelta(seconds=1),
        max_attempts=2,
    ) is None
    second_claim = storage.claim_delivery(
        saved.id,
        "2026-08-19",
        retry_at,
        max_attempts=2,
    )
    assert second_claim is not None
    assert second_claim.attempt_count == 2
    assert storage.fail_delivery(
        second_claim,
        retry_at,
        "still failing",
        next_attempt_at=None,
    )

    record = storage.get_delivery(saved.id, "2026-08-19")
    assert record is not None
    assert record.status == "failed"
    assert record.attempt_count == 2
    assert record.error == "still failing"
    assert record.failed_at == retry_at.isoformat()
    assert storage.claim_delivery(
        saved.id,
        "2026-08-19",
        retry_at + timedelta(hours=1),
        max_attempts=2,
    ) is None


def test_open_delivery_expires_after_delivery_window(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = storage.add_subscription(subscription())
    assert saved.id is not None
    now = datetime(2026, 8, 20, 13, 55, tzinfo=UTC)
    window_end = datetime(2026, 8, 20, 14, 0, tzinfo=UTC)
    claim = storage.claim_delivery(
        saved.id,
        "2026-08-19",
        now,
        window_end=window_end,
    )
    assert claim is not None
    assert storage.fail_delivery(
        claim,
        now,
        "temporary",
        next_attempt_at=now + timedelta(minutes=1),
    )

    assert storage.expire_open_deliveries(window_end) == 1
    record = storage.get_delivery(saved.id, "2026-08-19")
    assert record is not None
    assert record.status == "failed"
    assert record.failed_at == window_end.isoformat()
    assert record.next_attempt_at is None


def test_sending_delivery_expires_after_window_and_lease(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = storage.add_subscription(subscription())
    assert saved.id is not None
    now = datetime(2026, 8, 20, 13, 55, tzinfo=UTC)
    window_end = datetime(2026, 8, 20, 14, 0, tzinfo=UTC)
    claim = storage.claim_delivery(
        saved.id,
        "2026-08-19",
        now,
        stale_after=timedelta(minutes=10),
        window_end=window_end,
    )
    assert claim is not None

    assert storage.expire_open_deliveries(window_end) == 0
    assert storage.expire_open_deliveries(now + timedelta(minutes=10)) == 1
    record = storage.get_delivery(saved.id, "2026-08-19")
    assert record is not None
    assert record.status == "failed"


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


def test_subscription_active_state_is_scoped_idempotent_and_persistent(tmp_path) -> None:
    database_path = tmp_path / "bot.sqlite3"
    storage = Storage(database_path)
    storage.initialize()
    saved = storage.add_subscription(subscription())
    assert saved.id is not None

    paused = storage.set_subscription_active(saved.id, saved.target, False)
    assert paused == replace(saved, active=False)
    assert storage.set_subscription_active(saved.id, saved.target, False) == paused
    assert storage.set_subscription_active(saved.id, "another-chat", True) is None
    assert storage.set_subscription_active(999, saved.target, True) is None

    restarted_storage = Storage(database_path)
    restarted_storage.initialize()
    assert restarted_storage.get_subscription(saved.id, saved.target) == paused
    assert restarted_storage.set_subscription_active(saved.id, saved.target, True) == saved
