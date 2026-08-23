from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Subscription:
    id: int | None
    channel: str
    target: str
    repository: str
    digest_path: str
    ref: str
    token_env: str | None
    timezone: str
    send_time: str
    created_by: int
    active: bool = True


@dataclass(frozen=True, slots=True)
class DigestDocument:
    date: str
    body: str | None


@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    subscription_id: int
    digest_date: str
    attempt_count: int
    token: str


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    subscription_id: int
    digest_date: str
    status: str
    attempt_count: int
    attempted_at: str
    next_attempt_at: str | None
    sent_at: str | None
    failed_at: str | None
    error: str | None
    claim_token: str | None
    window_end_at: str
    lease_expires_at: str | None


@dataclass(frozen=True, slots=True)
class DeliveryStats:
    total: int
    sent: int
    failed: int
    retrying: int
    sending: int
    attempts: int
    latest: DeliveryRecord | None


@dataclass(frozen=True, slots=True)
class FailureNotificationClaim:
    subscription_id: int
    digest_date: str
    channel: str
    target: str
    reason: str
    delivery_attempt_count: int
    attempt_count: int
    token: str
