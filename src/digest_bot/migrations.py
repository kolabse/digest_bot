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

DELIVERIES_SCHEMA = """
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

def _normalize_schema_sql(value: str) -> str:
    return " ".join(value.split()).casefold()


_EXPECTED_SCHEMA = {
    ("table", "subscriptions"): _normalize_schema_sql(SUBSCRIPTIONS_SCHEMA),
    ("table", "deliveries"): _normalize_schema_sql(DELIVERIES_SCHEMA),
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


def _validate_schema(connection: sqlite3.Connection, label: str) -> None:
    if _user_schema(connection) != _EXPECTED_SCHEMA:
        raise SchemaMigrationError(f"Database does not match the {label} schema")


def _migrate_to_version_1(connection: sqlite3.Connection) -> None:
    schema = _user_schema(connection)
    if not schema:
        connection.execute(SUBSCRIPTIONS_SCHEMA)
        connection.execute(DELIVERIES_SCHEMA)
        return
    _validate_schema(connection, "v0.1.0")


MIGRATIONS: dict[int, Migration] = {1: _migrate_to_version_1}
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
        _validate_schema(connection, f"version {LATEST_SCHEMA_VERSION}")
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise SchemaMigrationError("Foreign key violations found after database migration")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
