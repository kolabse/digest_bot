from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .migrations import migrate
from .models import DeliveryClaim, DeliveryRecord, Subscription


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

    @staticmethod
    def _delivery_from_row(row: sqlite3.Row) -> DeliveryRecord:
        return DeliveryRecord(
            subscription_id=row["subscription_id"],
            digest_date=row["digest_date"],
            status=row["status"],
            attempt_count=row["attempt_count"],
            attempted_at=row["attempted_at"],
            next_attempt_at=row["next_attempt_at"],
            sent_at=row["sent_at"],
            failed_at=row["failed_at"],
            error=row["error"],
            claim_token=row["claim_token"],
            window_end_at=row["window_end_at"],
            lease_expires_at=row["lease_expires_at"],
        )

    def get_delivery(self, subscription_id: int, digest_date: str) -> DeliveryRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM deliveries
                WHERE subscription_id = ? AND digest_date = ?
                """,
                (subscription_id, digest_date),
            ).fetchone()
            return self._delivery_from_row(row) if row is not None else None

    def claim_delivery(
        self,
        subscription_id: int,
        digest_date: str,
        now: datetime,
        *,
        max_attempts: int = 5,
        stale_after: timedelta = timedelta(minutes=10),
        window_end: datetime | None = None,
    ) -> DeliveryClaim | None:
        token = uuid.uuid4().hex
        attempted_at = now.isoformat()
        lease_expires_at = (now + stale_after).isoformat()
        window_end_at = (window_end or now + timedelta(days=1)).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO deliveries (
                    subscription_id, digest_date, status, attempted_at,
                    attempt_count, next_attempt_at, claim_token,
                    window_end_at, lease_expires_at
                )
                VALUES (?, ?, 'sending', ?, 1, NULL, ?, ?, ?)
                ON CONFLICT (subscription_id, digest_date) DO NOTHING
                """,
                (
                    subscription_id,
                    digest_date,
                    attempted_at,
                    token,
                    window_end_at,
                    lease_expires_at,
                ),
            )
            if cursor.rowcount > 0:
                return DeliveryClaim(subscription_id, digest_date, 1, token)

            connection.execute(
                """
                UPDATE deliveries SET
                    status = 'failed', failed_at = ?, next_attempt_at = NULL,
                    claim_token = NULL, lease_expires_at = NULL,
                    error = COALESCE(error, 'Delivery attempt lease expired')
                WHERE subscription_id = ? AND digest_date = ?
                  AND attempt_count >= ?
                  AND (
                    (status = 'retrying' AND next_attempt_at <= ?)
                    OR (status = 'sending' AND lease_expires_at <= ?)
                  )
                """,
                (
                    attempted_at,
                    subscription_id,
                    digest_date,
                    max_attempts,
                    attempted_at,
                    attempted_at,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE deliveries SET
                    status = 'sending', attempted_at = ?, attempt_count = attempt_count + 1,
                    next_attempt_at = NULL, claim_token = ?, lease_expires_at = ?
                WHERE subscription_id = ? AND digest_date = ?
                  AND attempt_count < ?
                  AND (
                    (status = 'retrying' AND next_attempt_at <= ?)
                    OR (status = 'sending' AND lease_expires_at <= ?)
                  )
                  AND window_end_at > ?
                """,
                (
                    attempted_at,
                    token,
                    lease_expires_at,
                    subscription_id,
                    digest_date,
                    max_attempts,
                    attempted_at,
                    attempted_at,
                    attempted_at,
                ),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                """
                SELECT attempt_count FROM deliveries
                WHERE subscription_id = ? AND digest_date = ? AND claim_token = ?
                """,
                (subscription_id, digest_date, token),
            ).fetchone()
            if row is None:
                return None
            return DeliveryClaim(subscription_id, digest_date, int(row[0]), token)

    def complete_delivery(self, claim: DeliveryClaim, now: datetime) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE deliveries SET
                    status = 'sent', sent_at = ?, error = NULL,
                    next_attempt_at = NULL, failed_at = NULL, claim_token = NULL,
                    lease_expires_at = NULL
                WHERE subscription_id = ? AND digest_date = ?
                  AND status = 'sending' AND claim_token = ?
                """,
                (
                    now.isoformat(),
                    claim.subscription_id,
                    claim.digest_date,
                    claim.token,
                ),
            )
            return cursor.rowcount > 0

    def fail_delivery(
        self,
        claim: DeliveryClaim,
        now: datetime,
        error: str,
        *,
        next_attempt_at: datetime | None,
    ) -> bool:
        status = "retrying" if next_attempt_at is not None else "failed"
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE deliveries SET
                    status = ?, error = ?, next_attempt_at = ?, failed_at = ?,
                    claim_token = NULL, lease_expires_at = NULL
                WHERE subscription_id = ? AND digest_date = ?
                  AND status = 'sending' AND claim_token = ?
                """,
                (
                    status,
                    error[:2000],
                    next_attempt_at.isoformat() if next_attempt_at is not None else None,
                    now.isoformat() if next_attempt_at is None else None,
                    claim.subscription_id,
                    claim.digest_date,
                    claim.token,
                ),
            )
            return cursor.rowcount > 0

    def expire_open_deliveries(self, now: datetime) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE deliveries SET
                    status = 'failed', failed_at = ?, next_attempt_at = NULL,
                    claim_token = NULL, lease_expires_at = NULL,
                    error = COALESCE(error, 'Delivery window expired')
                WHERE (status = 'retrying' AND window_end_at <= ?)
                   OR (status = 'sending' AND window_end_at <= ? AND lease_expires_at <= ?)
                """,
                (
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            return cursor.rowcount
