from __future__ import annotations

import sqlite3
from collections.abc import Callable


class SchemaMigrationError(RuntimeError):
    pass


class UnsupportedSchemaVersionError(SchemaMigrationError):
    pass


Migration = Callable[[sqlite3.Connection], None]

SUBSCRIPTIONS_SCHEMA = """
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
)
"""

DELIVERIES_SCHEMA_V1 = """
CREATE TABLE deliveries (
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    digest_date TEXT NOT NULL,
    status TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    sent_at TEXT,
    error TEXT,
    PRIMARY KEY (subscription_id, digest_date)
)
"""

DELIVERIES_SCHEMA = """
CREATE TABLE deliveries (
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    digest_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('sending', 'retrying', 'sent', 'failed')),
    attempted_at TEXT NOT NULL,
    sent_at TEXT,
    error TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TEXT,
    failed_at TEXT,
    claim_token TEXT,
    window_end_at TEXT NOT NULL,
    lease_expires_at TEXT,
    PRIMARY KEY (subscription_id, digest_date),
    CHECK (
        (status = 'sending' AND claim_token IS NOT NULL
            AND lease_expires_at IS NOT NULL AND next_attempt_at IS NULL
            AND sent_at IS NULL AND failed_at IS NULL)
        OR (status = 'retrying' AND claim_token IS NULL
            AND lease_expires_at IS NULL AND next_attempt_at IS NOT NULL
            AND sent_at IS NULL AND failed_at IS NULL)
        OR (status = 'sent' AND claim_token IS NULL
            AND lease_expires_at IS NULL AND next_attempt_at IS NULL
            AND sent_at IS NOT NULL AND failed_at IS NULL)
        OR (status = 'failed' AND claim_token IS NULL
            AND lease_expires_at IS NULL AND next_attempt_at IS NULL
            AND sent_at IS NULL AND failed_at IS NOT NULL)
    )
)
"""

DELIVERIES_RETRY_INDEX = """
CREATE INDEX deliveries_retry_due_idx
ON deliveries (status, next_attempt_at, window_end_at, lease_expires_at)
"""

DELIVERIES_RETENTION_INDEX = """
CREATE INDEX deliveries_retention_idx
ON deliveries (status, sent_at, failed_at)
"""

FAILURE_NOTIFICATIONS_SCHEMA = """
CREATE TABLE delivery_failure_notifications (
    subscription_id INTEGER NOT NULL,
    digest_date TEXT NOT NULL,
    channel TEXT NOT NULL,
    target TEXT NOT NULL,
    reason TEXT NOT NULL,
    delivery_attempt_count INTEGER NOT NULL CHECK (delivery_attempt_count >= 1),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'sending', 'retrying', 'sent', 'abandoned')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TEXT,
    claim_token TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    error TEXT,
    PRIMARY KEY (subscription_id, digest_date),
    FOREIGN KEY (subscription_id, digest_date)
        REFERENCES deliveries(subscription_id, digest_date) ON DELETE CASCADE,
    CHECK (
        (status = 'pending' AND attempt_count = 0 AND next_attempt_at IS NULL
            AND claim_token IS NULL AND lease_expires_at IS NULL AND sent_at IS NULL)
        OR (status = 'sending' AND attempt_count >= 1 AND next_attempt_at IS NULL
            AND claim_token IS NOT NULL AND lease_expires_at IS NOT NULL
            AND sent_at IS NULL)
        OR (status = 'retrying' AND attempt_count >= 1 AND next_attempt_at IS NOT NULL
            AND claim_token IS NULL AND lease_expires_at IS NULL AND sent_at IS NULL)
        OR (status = 'sent' AND attempt_count >= 1 AND next_attempt_at IS NULL
            AND claim_token IS NULL AND lease_expires_at IS NULL AND sent_at IS NOT NULL)
        OR (status = 'abandoned' AND attempt_count >= 1 AND next_attempt_at IS NULL
            AND claim_token IS NULL AND lease_expires_at IS NULL AND sent_at IS NULL)
    )
)
"""

FAILURE_NOTIFICATIONS_DUE_INDEX = """
CREATE INDEX delivery_failure_notifications_due_idx
ON delivery_failure_notifications (status, next_attempt_at, lease_expires_at)
"""

def _normalize_schema_sql(value: str) -> str:
    return " ".join(value.split()).casefold()


_EXPECTED_SCHEMAS = {
    1: {
        ("table", "subscriptions"): _normalize_schema_sql(SUBSCRIPTIONS_SCHEMA),
        ("table", "deliveries"): _normalize_schema_sql(DELIVERIES_SCHEMA_V1),
    },
    2: {
        ("table", "subscriptions"): _normalize_schema_sql(SUBSCRIPTIONS_SCHEMA),
        ("table", "deliveries"): _normalize_schema_sql(DELIVERIES_SCHEMA),
        ("index", "deliveries_retry_due_idx"): _normalize_schema_sql(
            DELIVERIES_RETRY_INDEX
        ),
    },
    3: {
        ("table", "subscriptions"): _normalize_schema_sql(SUBSCRIPTIONS_SCHEMA),
        ("table", "deliveries"): _normalize_schema_sql(DELIVERIES_SCHEMA),
        ("index", "deliveries_retry_due_idx"): _normalize_schema_sql(
            DELIVERIES_RETRY_INDEX
        ),
        ("index", "deliveries_retention_idx"): _normalize_schema_sql(
            DELIVERIES_RETENTION_INDEX
        ),
        ("table", "delivery_failure_notifications"): _normalize_schema_sql(
            FAILURE_NOTIFICATIONS_SCHEMA
        ),
        ("index", "delivery_failure_notifications_due_idx"): _normalize_schema_sql(
            FAILURE_NOTIFICATIONS_DUE_INDEX
        ),
    },
}


def _user_schema(connection: sqlite3.Connection) -> dict[tuple[str, str], str]:
    rows = connection.execute(
        """
        SELECT type, name, sql FROM sqlite_schema
        WHERE name NOT LIKE 'sqlite_%'
        """
    )
    return {
        (str(row[0]), str(row[1])): _normalize_schema_sql(str(row[2] or ""))
        for row in rows
    }


def _validate_schema(connection: sqlite3.Connection, version: int, label: str) -> None:
    if _user_schema(connection) != _EXPECTED_SCHEMAS[version]:
        raise SchemaMigrationError(f"Database does not match the {label} schema")


def _migrate_to_version_1(connection: sqlite3.Connection) -> None:
    schema = _user_schema(connection)
    if not schema:
        connection.execute(SUBSCRIPTIONS_SCHEMA)
        connection.execute(DELIVERIES_SCHEMA_V1)
        return
    _validate_schema(connection, 1, "v0.1.0")


def _migrate_to_version_2(connection: sqlite3.Connection) -> None:
    _validate_schema(connection, 1, "version 1")
    malformed = connection.execute(
        """
        SELECT 1 FROM deliveries
        WHERE status NOT IN ('sending', 'sent')
           OR (status = 'sent' AND sent_at IS NULL)
        LIMIT 1
        """
    ).fetchone()
    if malformed is not None:
        raise SchemaMigrationError("Version 1 deliveries contain invalid states")
    connection.execute("ALTER TABLE deliveries RENAME TO deliveries_v1")
    connection.execute(DELIVERIES_SCHEMA)
    connection.execute(
        """
        INSERT INTO deliveries (
            subscription_id, digest_date, status, attempted_at, sent_at, error,
            attempt_count, next_attempt_at, failed_at, claim_token,
            window_end_at, lease_expires_at
        )
        SELECT
            subscription_id,
            digest_date,
            CASE status WHEN 'sent' THEN 'sent' ELSE 'failed' END,
            attempted_at,
            sent_at,
            CASE
                WHEN status = 'sending' AND error IS NULL
                THEN 'Ambiguous in-progress delivery closed during schema upgrade'
                ELSE error
            END,
            1,
            NULL,
            CASE status WHEN 'sending' THEN attempted_at ELSE NULL END,
            NULL,
            COALESCE(sent_at, attempted_at),
            NULL
        FROM deliveries_v1
        """
    )
    connection.execute("DROP TABLE deliveries_v1")
    connection.execute(DELIVERIES_RETRY_INDEX)


def _migrate_to_version_3(connection: sqlite3.Connection) -> None:
    _validate_schema(connection, 2, "version 2")
    connection.execute(DELIVERIES_RETENTION_INDEX)
    connection.execute(FAILURE_NOTIFICATIONS_SCHEMA)
    connection.execute(FAILURE_NOTIFICATIONS_DUE_INDEX)


MIGRATIONS: dict[int, Migration] = {
    1: _migrate_to_version_1,
    2: _migrate_to_version_2,
    3: _migrate_to_version_3,
}
LATEST_SCHEMA_VERSION = max(MIGRATIONS)


def migrate(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("BEGIN IMMEDIATE")
    try:
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version > LATEST_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"Database schema version {current_version} is newer than supported "
                f"version {LATEST_SCHEMA_VERSION}"
            )
        for version in range(current_version + 1, LATEST_SCHEMA_VERSION + 1):
            migration = MIGRATIONS.get(version)
            if migration is None:
                raise SchemaMigrationError(f"Missing database migration for version {version}")
            migration(connection)
            connection.execute(f"PRAGMA user_version = {version}")
        _validate_schema(
            connection,
            LATEST_SCHEMA_VERSION,
            f"version {LATEST_SCHEMA_VERSION}",
        )
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise SchemaMigrationError("Foreign key violations found after database migration")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
