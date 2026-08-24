from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .channels.base import DeliveryChannel
from .digest import extract_digest, render_message
from .errors import DeliveryOperationError
from .github import RepositoryContentsSource
from .schedule import digest_date_for_send_time, due_delivery
from .storage import Storage

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 5
    initial_delay_seconds: int = 60
    max_delay_seconds: int = 3600
    claim_lease_seconds: int = 3600

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.initial_delay_seconds < 1:
            raise ValueError("initial_delay_seconds must be at least 1")
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise ValueError("max_delay_seconds must not be less than initial_delay_seconds")
        if self.claim_lease_seconds < 60:
            raise ValueError("claim_lease_seconds must be at least 60")

    def delay_seconds(self, attempt_count: int) -> float:
        return float(
            min(
                self.initial_delay_seconds * (2 ** max(attempt_count - 1, 0)),
                self.max_delay_seconds,
            )
        )


class DeliveryService:
    def __init__(
        self,
        storage: Storage,
        source: RepositoryContentsSource,
        channels: dict[str, DeliveryChannel],
        retry_policy: RetryPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        history_retention_days: int = 90,
        cleanup_batch_size: int = 500,
    ) -> None:
        self._storage = storage
        self._source = source
        self._channels = channels
        self._retry_policy = retry_policy or RetryPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))
        if history_retention_days < 1:
            raise ValueError("history_retention_days must be at least 1")
        if cleanup_batch_size < 1:
            raise ValueError("cleanup_batch_size must be at least 1")
        self._history_retention_days = history_retention_days
        self._cleanup_batch_size = cleanup_batch_size

    @staticmethod
    def _failure_notification_message(
        subscription_id: int,
        digest_date: str,
        reason: str,
        attempt_count: int,
    ) -> str:
        reasons = {
            "permanent_error": "ошибка не допускает повторной попытки",
            "attempts_exhausted": "исчерпаны повторные попытки",
            "window_expired": "завершилось окно доставки",
        }
        explanation = reasons.get(reason, "доставка завершилась с ошибкой")
        return (
            f"⚠️ <b>Рассылка #{subscription_id} не доставлена</b>\n\n"
            f"Дата дайджеста: {digest_date}\n"
            f"Причина: {explanation}.\n"
            f"Попыток: {attempt_count}.\n\n"
            f"Проверьте источник командой /preview {subscription_id}."
        )

    async def _dispatch_failure_notifications(self, now: datetime | None) -> None:
        def current_time() -> datetime:
            return now if now is not None else self._clock()

        scan_time = current_time()
        abandoned = self._storage.abandon_exhausted_failure_notifications(
            scan_time,
            max_attempts=self._retry_policy.max_attempts,
        )
        for subscription_id, digest_date, channel in abandoned:
            LOGGER.error(
                "Failure notification abandoned after attempt limit: "
                "subscription_id=%s digest_date=%s channel=%s max_attempts=%s",
                subscription_id,
                digest_date,
                channel,
                self._retry_policy.max_attempts,
            )

        for _ in range(100):
            claim_time = scan_time
            claim = self._storage.claim_failure_notification(
                claim_time,
                max_attempts=self._retry_policy.max_attempts,
                stale_after=timedelta(seconds=self._retry_policy.claim_lease_seconds),
            )
            if claim is None:
                return
            try:
                channel = self._channels[claim.channel]
                await channel.send(
                    claim.target,
                    self._failure_notification_message(
                        claim.subscription_id,
                        claim.digest_date,
                        claim.reason,
                        claim.delivery_attempt_count,
                    ),
                )
            except Exception as exc:
                operation_error = exc if isinstance(exc, DeliveryOperationError) else None
                retryable = operation_error.retryable if operation_error else False
                failure_time = current_time()
                scan_time = failure_time
                delay = self._retry_policy.delay_seconds(claim.attempt_count)
                retry_after = (
                    operation_error.retry_after_seconds if operation_error else None
                )
                if retry_after is not None and math.isfinite(retry_after) and retry_after > 0:
                    delay = max(delay, retry_after)
                exhausted = claim.attempt_count >= self._retry_policy.max_attempts
                next_attempt_at = (
                    failure_time + timedelta(seconds=delay)
                    if retryable and not exhausted
                    else None
                )
                transitioned = self._storage.fail_failure_notification(
                    claim,
                    failure_time,
                    type(exc).__name__,
                    next_attempt_at=next_attempt_at,
                )
                if transitioned:
                    LOGGER.warning(
                        "Failure notification was not delivered: subscription_id=%s "
                        "digest_date=%s channel=%s attempt=%s/%s retryable=%s",
                        claim.subscription_id,
                        claim.digest_date,
                        claim.channel,
                        claim.attempt_count,
                        self._retry_policy.max_attempts,
                        retryable,
                    )
                continue
            completion_time = current_time()
            scan_time = completion_time
            completed = self._storage.complete_failure_notification(claim, completion_time)
            if completed:
                LOGGER.info(
                    "Failure notification sent: subscription_id=%s digest_date=%s channel=%s",
                    claim.subscription_id,
                    claim.digest_date,
                    claim.channel,
                )

    async def dispatch_due(self, now: datetime | None = None) -> None:
        def current_time() -> datetime:
            return now if now is not None else self._clock()

        maintenance_time = current_time()
        expired = self._storage.expire_open_deliveries(maintenance_time)
        if expired:
            LOGGER.error("Expired open deliveries after window close: count=%s", expired)
        for subscription in self._storage.list_subscriptions():
            if not subscription.active or subscription.id is None:
                continue
            claim_time = current_time()
            maintenance_time = claim_time
            due = due_delivery(subscription.timezone, subscription.send_time, claim_time)
            if due is None:
                continue
            digest_date = due.digest_date.isoformat()
            claim = self._storage.claim_delivery(
                subscription.id,
                digest_date,
                claim_time,
                max_attempts=self._retry_policy.max_attempts,
                stale_after=timedelta(seconds=self._retry_policy.claim_lease_seconds),
                window_end=due.window_end.astimezone(UTC),
            )
            if claim is None:
                continue
            try:
                markdown = await self._source.read(
                    subscription.repository,
                    subscription.digest_path,
                    subscription.ref,
                    subscription.token_env,
                )
                document = extract_digest(markdown, due.digest_date)
                custom_variants = tuple(
                    self._storage.list_message_variants(subscription.id) or ()
                )
                message = render_message(
                    document,
                    digest_is_today=due.digest_date == due.local_now.date(),
                    selection_key=f"{subscription.id}:{digest_date}",
                    custom_variants=custom_variants,
                )
                channel = self._channels[subscription.channel]
                await channel.send(subscription.target, message)
            except Exception as exc:
                operation_error = exc if isinstance(exc, DeliveryOperationError) else None
                retryable = operation_error.retryable if operation_error else False
                retry_after = (
                    operation_error.retry_after_seconds if operation_error else None
                )
                failure_time = current_time()
                maintenance_time = failure_time
                delay = self._retry_policy.delay_seconds(claim.attempt_count)
                if retry_after is not None and math.isfinite(retry_after) and retry_after > 0:
                    delay = max(delay, retry_after)
                window_end = due.window_end.astimezone(UTC)
                remaining_seconds = max((window_end - failure_time).total_seconds(), 0)
                delay = min(delay, remaining_seconds)
                next_attempt_at = failure_time + timedelta(seconds=delay)
                exhausted = claim.attempt_count >= self._retry_policy.max_attempts
                if not retryable or exhausted or next_attempt_at >= window_end:
                    next_attempt_at = None
                error = type(exc).__name__
                terminal_reason = "permanent_error"
                if retryable and exhausted:
                    terminal_reason = "attempts_exhausted"
                elif retryable and next_attempt_at is None:
                    terminal_reason = "window_expired"
                transitioned = self._storage.fail_delivery(
                    claim,
                    failure_time,
                    error,
                    next_attempt_at=next_attempt_at,
                    reason=terminal_reason,
                )
                if not transitioned:
                    LOGGER.warning(
                        "Ignored failure from stale delivery claim: "
                        "subscription_id=%s digest_date=%s attempt=%s",
                        subscription.id,
                        digest_date,
                        claim.attempt_count,
                    )
                elif next_attempt_at is not None:
                    LOGGER.warning(
                        "Delivery retry scheduled: subscription_id=%s digest_date=%s "
                        "channel=%s attempt=%s/%s next_attempt_at=%s error_type=%s",
                        subscription.id,
                        digest_date,
                        subscription.channel,
                        claim.attempt_count,
                        self._retry_policy.max_attempts,
                        next_attempt_at.isoformat(),
                        type(exc).__name__,
                    )
                else:
                    LOGGER.error(
                        "Delivery failed permanently: subscription_id=%s digest_date=%s "
                        "channel=%s attempt=%s/%s retryable=%s",
                        subscription.id,
                        digest_date,
                        subscription.channel,
                        claim.attempt_count,
                        self._retry_policy.max_attempts,
                        retryable,
                    )
                continue

            completion_time = current_time()
            maintenance_time = completion_time
            completed = self._storage.complete_delivery(claim, completion_time)
            if completed:
                LOGGER.info(
                    "Sent digest: digest_date=%s subscription_id=%s channel=%s attempt=%s",
                    digest_date,
                    subscription.id,
                    subscription.channel,
                    claim.attempt_count,
                )
            else:
                LOGGER.warning(
                    "Delivery completed after claim was lost: subscription_id=%s "
                    "digest_date=%s attempt=%s",
                    subscription.id,
                    digest_date,
                    claim.attempt_count,
                )

        await self._dispatch_failure_notifications(now)
        cutoff = maintenance_time - timedelta(days=self._history_retention_days)
        deleted = self._storage.cleanup_deliveries(
            cutoff,
            batch_size=self._cleanup_batch_size,
        )
        if deleted:
            LOGGER.info("Cleaned terminal delivery history: count=%s", deleted)

    async def preview(self, subscription_id: int, target: str, now: datetime | None = None) -> str:
        subscriptions = {
            item.id: item for item in self._storage.list_subscriptions(target=target)
        }
        subscription = subscriptions.get(subscription_id)
        if subscription is None:
            raise LookupError("Рассылка не найдена в этом чате.")
        current = now or datetime.now(UTC)
        from zoneinfo import ZoneInfo

        local = current.astimezone(ZoneInfo(subscription.timezone))
        digest_date = digest_date_for_send_time(local.date(), subscription.send_time)
        markdown = await self._source.read(
            subscription.repository,
            subscription.digest_path,
            subscription.ref,
            subscription.token_env,
        )
        return render_message(
            extract_digest(markdown, digest_date),
            digest_is_today=digest_date == local.date(),
            selection_key=f"{subscription.id}:{digest_date.isoformat()}",
            custom_variants=tuple(
                self._storage.list_message_variants(subscription.id or 0) or ()
            ),
        )
