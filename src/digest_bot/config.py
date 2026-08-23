from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(RuntimeError):
    pass


def _parse_ids(raw: str) -> frozenset[int]:
    if not raw.strip():
        return frozenset()
    try:
        return frozenset(int(value.strip()) for value in raw.split(",") if value.strip())
    except ValueError as exc:
        raise ConfigurationError(
            "BOT_ADMIN_USER_IDS must contain comma-separated integers"
        ) from exc


def _parse_hosts(raw: str) -> frozenset[str]:
    return frozenset(value.strip().lower() for value in raw.split(",") if value.strip())


def _parse_int(name: str, default: int, *, minimum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    telegram_proxy_url: str | None
    gitlab_allowed_hosts: frozenset[str]
    database_path: Path
    admin_user_ids: frozenset[int]
    default_timezone: str
    scheduler_interval_seconds: int
    delivery_max_attempts: int = 5
    delivery_retry_initial_seconds: int = 60
    delivery_retry_max_seconds: int = 3600
    delivery_claim_lease_seconds: int = 3600

    @classmethod
    def from_env(cls) -> Settings:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ConfigurationError("TELEGRAM_BOT_TOKEN is required")
        interval = _parse_int("SCHEDULER_INTERVAL_SECONDS", 30, minimum=10)
        max_attempts = _parse_int("DELIVERY_MAX_ATTEMPTS", 5, minimum=1)
        retry_initial = _parse_int("DELIVERY_RETRY_INITIAL_SECONDS", 60, minimum=1)
        retry_max = _parse_int("DELIVERY_RETRY_MAX_SECONDS", 3600, minimum=1)
        claim_lease = _parse_int("DELIVERY_CLAIM_LEASE_SECONDS", 3600, minimum=60)
        if retry_max < retry_initial:
            raise ConfigurationError(
                "DELIVERY_RETRY_MAX_SECONDS must be greater than or equal to "
                "DELIVERY_RETRY_INITIAL_SECONDS"
            )
        return cls(
            telegram_bot_token=token,
            telegram_proxy_url=os.getenv("TELEGRAM_PROXY_URL", "").strip() or None,
            gitlab_allowed_hosts=_parse_hosts(os.getenv("GITLAB_ALLOWED_HOSTS", "")),
            database_path=Path(os.getenv("DATABASE_PATH", "data/digest-bot.sqlite3")),
            admin_user_ids=_parse_ids(os.getenv("BOT_ADMIN_USER_IDS", "")),
            default_timezone=os.getenv("DEFAULT_TIMEZONE", "Asia/Yekaterinburg"),
            scheduler_interval_seconds=interval,
            delivery_max_attempts=max_attempts,
            delivery_retry_initial_seconds=retry_initial,
            delivery_retry_max_seconds=retry_max,
            delivery_claim_lease_seconds=claim_lease,
        )
