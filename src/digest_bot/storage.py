from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .models import Subscription

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
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
CREATE TABLE IF NOT EXISTS deliveries (
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    digest_date TEXT NOT NULL,
    status TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    sent_at TEXT,
    error TEXT,
    PRIMARY KEY (subscription_id, digest_date)
);
"""


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Subscription:
        return Subscription(
            id=row["id"],
            channel=row["channel"],
            target=row["target"],
            repository=row["repository"],
            digest_path=row["digest_path"],
            ref=row["ref"],
            token_env=row["token_env"],
            timezone=row["timezone"],
            send_time=row["send_time"],
            created_by=row["created_by"],
            active=bool(row["active"]),
        )

    def add_subscription(self, subscription: Subscription) -> Subscription:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO subscriptions (
                    channel, target, repository, digest_path, ref, token_env,
                    timezone, send_time, created_by, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    subscription.channel,
                    subscription.target,
                    subscription.repository,
                    subscription.digest_path,
                    subscription.ref,
                    subscription.token_env,
                    subscription.timezone,
                    subscription.send_time,
                    subscription.created_by,
                    int(subscription.active),
                ),
            )
            subscription_id = int(cursor.lastrowid or 0)
        return Subscription(
            id=subscription_id,
            channel=subscription.channel,
            target=subscription.target,
            repository=subscription.repository,
            digest_path=subscription.digest_path,
            ref=subscription.ref,
            token_env=subscription.token_env,
            timezone=subscription.timezone,
            send_time=subscription.send_time,
            created_by=subscription.created_by,
            active=subscription.active,
        )

    def list_subscriptions(self, *, target: str | None = None) -> list[Subscription]:
        query = "SELECT * FROM subscriptions"
        params: tuple[str, ...] = ()
        if target is not None:
            query += " WHERE target = ?"
            params = (target,)
        query += " ORDER BY id"
        with self._connect() as connection:
            return [self._from_row(row) for row in connection.execute(query, params)]

    def delete_subscription(self, subscription_id: int, target: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM subscriptions WHERE id = ? AND target = ?",
                (subscription_id, target),
            )
            return cursor.rowcount > 0

    def claim_delivery(self, subscription_id: int, digest_date: str, now: datetime) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO deliveries (subscription_id, digest_date, status, attempted_at)
                VALUES (?, ?, 'sending', ?)
                ON CONFLICT (subscription_id, digest_date) DO NOTHING
                """,
                (subscription_id, digest_date, now.isoformat()),
            )
            if cursor.rowcount > 0:
                return True

            # Recover a claim left behind when the process stopped during delivery.
            stale_before = (now - timedelta(minutes=10)).isoformat()
            cursor = connection.execute(
                """
                UPDATE deliveries SET attempted_at = ?
                WHERE subscription_id = ? AND digest_date = ?
                  AND status = 'sending' AND attempted_at <= ?
                """,
                (now.isoformat(), subscription_id, digest_date, stale_before),
            )
            return cursor.rowcount > 0

    def complete_delivery(self, subscription_id: int, digest_date: str, now: datetime) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE deliveries SET status = 'sent', sent_at = ?, error = NULL
                WHERE subscription_id = ? AND digest_date = ?
                """,
                (now.isoformat(), subscription_id, digest_date),
            )

    def release_delivery(self, subscription_id: int, digest_date: str, error: str) -> None:
        # A failed claim is removed so the next scheduler pass can retry.
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM deliveries WHERE subscription_id = ? AND digest_date = ?",
                (subscription_id, digest_date),
            )
