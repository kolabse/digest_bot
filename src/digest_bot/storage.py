from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .migrations import migrate
from .models import Subscription


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
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            migrate(connection)
        finally:
            connection.close()

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

    def get_subscription(self, subscription_id: int, target: str) -> Subscription | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscriptions WHERE id = ? AND target = ?",
                (subscription_id, target),
            ).fetchone()
            return self._from_row(row) if row is not None else None

    def update_subscription(self, subscription: Subscription) -> Subscription | None:
        if subscription.id is None:
            raise ValueError("Saved subscription ID is required")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE subscriptions SET
                    repository = ?, digest_path = ?, ref = ?, token_env = ?,
                    timezone = ?, send_time = ?
                WHERE id = ? AND target = ?
                """,
                (
                    subscription.repository,
                    subscription.digest_path,
                    subscription.ref,
                    subscription.token_env,
                    subscription.timezone,
                    subscription.send_time,
                    subscription.id,
                    subscription.target,
                ),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM subscriptions WHERE id = ? AND target = ?",
                (subscription.id, subscription.target),
            ).fetchone()
            return self._from_row(row)

    def set_subscription_active(
        self,
        subscription_id: int,
        target: str,
        active: bool,
    ) -> Subscription | None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE subscriptions SET active = ?
                WHERE id = ? AND target = ?
                """,
                (int(active), subscription_id, target),
            )
            row = connection.execute(
                "SELECT * FROM subscriptions WHERE id = ? AND target = ?",
                (subscription_id, target),
            ).fetchone()
            return self._from_row(row) if row is not None else None

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
