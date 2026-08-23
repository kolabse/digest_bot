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
from digest_bot.storage import Storage

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
            "SELECT status, error FROM deliveries WHERE subscription_id = 1"
        ).fetchone() == ("sent", None)
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

    with pytest.raises(SchemaMigrationError, match="version 1 schema"):
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
        assert {"subscriptions", "deliveries"} <= tables


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
