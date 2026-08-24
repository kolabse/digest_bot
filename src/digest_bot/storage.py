from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .migrations import migrate
from .models import (
    MESSAGE_VARIANT_KINDS,
    DeliveryClaim,
    DeliveryRecord,
    DeliveryStats,
    FailureNotificationClaim,
    MessageVariant,
    Subscription,
)


class StaleMessageVariantsError(RuntimeError):
    pass


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
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
    def _message_variant_from_row(row: sqlite3.Row) -> MessageVariant:
        return MessageVariant(
            id=int(row["id"]),
            subscription_id=int(row["subscription_id"]),
            kind=str(row["kind"]),
            today_text=str(row["today_text"]),
            yesterday_text=str(row["yesterday_text"]),
            weight=int(row["weight"]),
        )

    def list_message_variants(
        self,
        subscription_id: int,
        *,
        target: str | None = None,
    ) -> list[MessageVariant] | None:
        with self._connect() as connection:
            if target is not None:
                exists = connection.execute(
                    "SELECT 1 FROM subscriptions WHERE id = ? AND target = ?",
                    (subscription_id, target),
                ).fetchone()
                if exists is None:
                    return None
            rows = connection.execute(
                """
                SELECT * FROM message_variants
                WHERE subscription_id = ?
                ORDER BY kind, id
                """,
                (subscription_id,),
            )
            return [self._message_variant_from_row(row) for row in rows]

    @staticmethod
    def message_variants_revision(variants: list[MessageVariant]) -> str:
        payload = [
            (item.kind, item.today_text, item.yesterday_text, item.weight)
            for item in variants
        ]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()

    def add_message_variant(
        self,
        subscription_id: int,
        target: str,
        kind: str,
        today_text: str,
        yesterday_text: str,
        weight: int,
    ) -> MessageVariant | None:
        today_text = today_text.strip()
        yesterday_text = yesterday_text.strip()
        if kind not in MESSAGE_VARIANT_KINDS:
            raise ValueError("Unknown message variant kind")
        if not 1 <= len(today_text) <= 500 or not 1 <= len(yesterday_text) <= 500:
            raise ValueError("Message variant text must contain 1 to 500 characters")
        if not 1 <= weight <= 100:
            raise ValueError("Message variant weight must be between 1 and 100")
        with self._connect(immediate=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM subscriptions WHERE id = ? AND target = ?",
                (subscription_id, target),
            ).fetchone()
            if exists is None:
                return None
            count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM message_variants
                    WHERE subscription_id = ? AND kind = ?
                    """,
                    (subscription_id, kind),
                ).fetchone()[0]
            )
            if count >= 20:
                raise ValueError("A message category can contain at most 20 variants")
            cursor = connection.execute(
                """
                INSERT INTO message_variants (
                    subscription_id, kind, today_text, yesterday_text, weight
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (subscription_id, kind, today_text, yesterday_text, weight),
            )
            variant_id = int(cursor.lastrowid or 0)
            row = connection.execute(
                "SELECT * FROM message_variants WHERE id = ?",
                (variant_id,),
            ).fetchone()
            return self._message_variant_from_row(row)

    def delete_message_variant(
        self,
        subscription_id: int,
        target: str,
        variant_id: int,
        kind: str,
    ) -> bool:
        if kind not in MESSAGE_VARIANT_KINDS:
            raise ValueError("Unknown message variant kind")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM message_variants
                WHERE id = ? AND subscription_id = ? AND kind = ?
                  AND EXISTS (
                    SELECT 1 FROM subscriptions
                    WHERE id = ? AND target = ?
                  )
                """,
                (variant_id, subscription_id, kind, subscription_id, target),
            )
            return cursor.rowcount > 0

    def clear_message_variants(
        self,
        subscription_id: int,
        target: str,
        *,
        kind: str | None = None,
    ) -> int | None:
        if kind is not None and kind not in MESSAGE_VARIANT_KINDS:
            raise ValueError("Unknown message variant kind")
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM subscriptions WHERE id = ? AND target = ?",
                (subscription_id, target),
            ).fetchone()
            if exists is None:
                return None
            params: list[object] = [subscription_id]
            condition = ""
            if kind is not None:
                condition = " AND kind = ?"
                params.append(kind)
            cursor = connection.execute(
                f"DELETE FROM message_variants WHERE subscription_id = ?{condition}",
                params,
            )
            return cursor.rowcount

    def replace_message_variants(
        self,
        subscription_id: int,
        target: str,
        variants: list[tuple[str, str, str, int]],
        *,
        expected_revision: str | None = None,
    ) -> list[MessageVariant] | None:
        counts: dict[str, int] = {}
        normalized: list[tuple[str, str, str, int]] = []
        for kind, today_text, yesterday_text, weight in variants:
            today_text = today_text.strip()
            yesterday_text = yesterday_text.strip()
            if kind not in MESSAGE_VARIANT_KINDS:
                raise ValueError("Unknown message variant kind")
            if not 1 <= len(today_text) <= 500 or not 1 <= len(yesterday_text) <= 500:
                raise ValueError("Message variant text must contain 1 to 500 characters")
            if not 1 <= weight <= 100:
                raise ValueError("Message variant weight must be between 1 and 100")
            counts[kind] = counts.get(kind, 0) + 1
            if counts[kind] > 20:
                raise ValueError("A message category can contain at most 20 variants")
            normalized.append((kind, today_text, yesterday_text, weight))
        with self._connect(immediate=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM subscriptions WHERE id = ? AND target = ?",
                (subscription_id, target),
            ).fetchone()
            if exists is None:
                return None
            current_rows = connection.execute(
                """
                SELECT * FROM message_variants
                WHERE subscription_id = ? ORDER BY kind, id
                """,
                (subscription_id,),
            ).fetchall()
            current = [self._message_variant_from_row(row) for row in current_rows]
            if (
                expected_revision is not None
                and self.message_variants_revision(current) != expected_revision
            ):
                raise StaleMessageVariantsError(
                    "Message variants changed after the draft was opened"
                )
            connection.execute(
                "DELETE FROM message_variants WHERE subscription_id = ?",
                (subscription_id,),
            )
            connection.executemany(
                """
                INSERT INTO message_variants (
                    subscription_id, kind, today_text, yesterday_text, weight
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (subscription_id, kind, today, yesterday, weight)
                    for kind, today, yesterday, weight in normalized
                ],
            )
            rows = connection.execute(
                """
                SELECT * FROM message_variants
                WHERE subscription_id = ? ORDER BY kind, id
                """,
                (subscription_id,),
            )
            return [self._message_variant_from_row(row) for row in rows]

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

    @staticmethod
    def _enqueue_failure_notification(
        connection: sqlite3.Connection,
        subscription_id: int,
        digest_date: str,
        reason: str,
        delivery_attempt_count: int,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO delivery_failure_notifications (
                subscription_id, digest_date, channel, target, reason,
                delivery_attempt_count, status, created_at
            )
            SELECT id, ?, channel, target, ?, ?, 'pending', ?
            FROM subscriptions
            WHERE id = ?
            ON CONFLICT (subscription_id, digest_date) DO NOTHING
            """,
            (
                digest_date,
                reason,
                delivery_attempt_count,
                created_at,
                subscription_id,
            ),
        )

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

            terminalized = connection.execute(
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
            if terminalized.rowcount > 0:
                terminal_row = connection.execute(
                    """
                    SELECT attempt_count FROM deliveries
                    WHERE subscription_id = ? AND digest_date = ?
                    """,
                    (subscription_id, digest_date),
                ).fetchone()
                self._enqueue_failure_notification(
                    connection,
                    subscription_id,
                    digest_date,
                    "attempts_exhausted",
                    int(terminal_row["attempt_count"]),
                    attempted_at,
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
        reason: str = "delivery_failed",
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
            transitioned = cursor.rowcount > 0
            if transitioned and next_attempt_at is None:
                self._enqueue_failure_notification(
                    connection,
                    claim.subscription_id,
                    claim.digest_date,
                    reason,
                    claim.attempt_count,
                    now.isoformat(),
                )
            return transitioned

    def expire_open_deliveries(self, now: datetime) -> int:
        with self._connect() as connection:
            candidates = connection.execute(
                """
                SELECT subscription_id, digest_date, attempt_count
                FROM deliveries
                WHERE (status = 'retrying' AND window_end_at <= ?)
                   OR (status = 'sending' AND window_end_at <= ? AND lease_expires_at <= ?)
                """,
                (now.isoformat(), now.isoformat(), now.isoformat()),
            ).fetchall()
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
            for row in candidates:
                self._enqueue_failure_notification(
                    connection,
                    int(row["subscription_id"]),
                    str(row["digest_date"]),
                    "window_expired",
                    int(row["attempt_count"]),
                    now.isoformat(),
                )
            return cursor.rowcount

    def delivery_stats(
        self,
        target: str,
        subscription_id: int | None = None,
    ) -> DeliveryStats | None:
        with self._connect() as connection:
            if subscription_id is not None:
                exists = connection.execute(
                    "SELECT 1 FROM subscriptions WHERE id = ? AND target = ?",
                    (subscription_id, target),
                ).fetchone()
                if exists is None:
                    return None
            params: list[object] = [target]
            subscription_filter = ""
            if subscription_id is not None:
                subscription_filter = " AND d.subscription_id = ?"
                params.append(subscription_id)
            aggregate = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN d.status = 'sent' THEN 1 ELSE 0 END) AS sent,
                    SUM(CASE WHEN d.status = 'failed' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN d.status = 'retrying' THEN 1 ELSE 0 END) AS retrying,
                    SUM(CASE WHEN d.status = 'sending' THEN 1 ELSE 0 END) AS sending,
                    COALESCE(SUM(d.attempt_count), 0) AS attempts
                FROM deliveries d
                JOIN subscriptions s ON s.id = d.subscription_id
                WHERE s.target = ?{subscription_filter}
                """,
                params,
            ).fetchone()
            latest_row = connection.execute(
                f"""
                SELECT d.* FROM deliveries d
                JOIN subscriptions s ON s.id = d.subscription_id
                WHERE s.target = ?{subscription_filter}
                ORDER BY d.digest_date DESC, d.attempted_at DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
            return DeliveryStats(
                total=int(aggregate["total"]),
                sent=int(aggregate["sent"] or 0),
                failed=int(aggregate["failed"] or 0),
                retrying=int(aggregate["retrying"] or 0),
                sending=int(aggregate["sending"] or 0),
                attempts=int(aggregate["attempts"]),
                latest=self._delivery_from_row(latest_row) if latest_row is not None else None,
            )

    def claim_failure_notification(
        self,
        now: datetime,
        *,
        max_attempts: int,
        stale_after: timedelta,
    ) -> FailureNotificationClaim | None:
        token = uuid.uuid4().hex
        timestamp = now.isoformat()
        lease_expires_at = (now + stale_after).isoformat()
        with self._connect() as connection:
            claimed = connection.execute(
                """
                UPDATE delivery_failure_notifications SET
                    status = 'sending', attempt_count = attempt_count + 1,
                    next_attempt_at = NULL, claim_token = ?, lease_expires_at = ?
                WHERE (subscription_id, digest_date) = (
                    SELECT subscription_id, digest_date
                    FROM delivery_failure_notifications
                    WHERE attempt_count < ? AND (
                        status = 'pending'
                        OR (status = 'retrying' AND next_attempt_at <= ?)
                        OR (status = 'sending' AND lease_expires_at <= ?)
                    )
                    ORDER BY created_at, subscription_id, digest_date
                    LIMIT 1
                )
                  AND attempt_count < ? AND (
                    status = 'pending'
                    OR (status = 'retrying' AND next_attempt_at <= ?)
                    OR (status = 'sending' AND lease_expires_at <= ?)
                  )
                RETURNING *
                """,
                (
                    token,
                    lease_expires_at,
                    max_attempts,
                    timestamp,
                    timestamp,
                    max_attempts,
                    timestamp,
                    timestamp,
                ),
            ).fetchone()
            if claimed is None:
                return None
            return FailureNotificationClaim(
                subscription_id=int(claimed["subscription_id"]),
                digest_date=str(claimed["digest_date"]),
                channel=str(claimed["channel"]),
                target=str(claimed["target"]),
                reason=str(claimed["reason"]),
                delivery_attempt_count=int(claimed["delivery_attempt_count"]),
                attempt_count=int(claimed["attempt_count"]),
                token=token,
            )

    def abandon_exhausted_failure_notifications(
        self,
        now: datetime,
        *,
        max_attempts: int,
    ) -> list[tuple[int, str, str]]:
        timestamp = now.isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                UPDATE delivery_failure_notifications SET
                    status = 'abandoned', next_attempt_at = NULL,
                    claim_token = NULL, lease_expires_at = NULL,
                    error = COALESCE(error, 'Notification attempt lease expired')
                WHERE status IN ('retrying', 'sending') AND attempt_count >= ?
                  AND ((status = 'retrying' AND next_attempt_at <= ?)
                    OR (status = 'sending' AND lease_expires_at <= ?))
                RETURNING subscription_id, digest_date, channel
                """,
                (max_attempts, timestamp, timestamp),
            ).fetchall()
            return [
                (int(row["subscription_id"]), str(row["digest_date"]), str(row["channel"]))
                for row in rows
            ]

    def complete_failure_notification(
        self,
        claim: FailureNotificationClaim,
        now: datetime,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE delivery_failure_notifications SET
                    status = 'sent', sent_at = ?, error = NULL,
                    claim_token = NULL, lease_expires_at = NULL
                WHERE subscription_id = ? AND digest_date = ?
                  AND status = 'sending' AND claim_token = ?
                """,
                (now.isoformat(), claim.subscription_id, claim.digest_date, claim.token),
            )
            return cursor.rowcount > 0

    def fail_failure_notification(
        self,
        claim: FailureNotificationClaim,
        now: datetime,
        error: str,
        *,
        next_attempt_at: datetime | None,
    ) -> bool:
        status = "retrying" if next_attempt_at is not None else "abandoned"
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE delivery_failure_notifications SET
                    status = ?, next_attempt_at = ?, error = ?,
                    claim_token = NULL, lease_expires_at = NULL
                WHERE subscription_id = ? AND digest_date = ?
                  AND status = 'sending' AND claim_token = ?
                """,
                (
                    status,
                    next_attempt_at.isoformat() if next_attempt_at is not None else None,
                    error[:500],
                    claim.subscription_id,
                    claim.digest_date,
                    claim.token,
                ),
            )
            return cursor.rowcount > 0

    def cleanup_deliveries(self, cutoff: datetime, *, batch_size: int) -> int:
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        cutoff_value = cutoff.astimezone(UTC)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM deliveries
                WHERE rowid IN (
                    SELECT d.rowid
                    FROM deliveries d
                    LEFT JOIN delivery_failure_notifications n
                      ON n.subscription_id = d.subscription_id
                     AND n.digest_date = d.digest_date
                    WHERE (
                        (d.status = 'sent' AND d.sent_at < ?)
                        OR (d.status = 'failed' AND d.failed_at < ?)
                    )
                      AND (n.status IS NULL OR n.status IN ('sent', 'abandoned'))
                    ORDER BY COALESCE(d.sent_at, d.failed_at), d.rowid
                    LIMIT ?
                )
                """,
                (cutoff_value.isoformat(), cutoff_value.isoformat(), batch_size),
            )
            return cursor.rowcount
