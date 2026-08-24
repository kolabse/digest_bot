from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from email.errors import HeaderParseError
from email.headerregistry import Address
from pathlib import Path


class ConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SmtpSettings:
    host: str
    port: int
    sender: str
    security: str
    username: str | None
    password: str | None
    timeout_seconds: int


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
    delivery_history_retention_days: int = 90
    delivery_cleanup_batch_size: int = 500
    smtp: SmtpSettings | None = None

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
        retention_days = _parse_int("DELIVERY_HISTORY_RETENTION_DAYS", 90, minimum=1)
        cleanup_batch_size = _parse_int("DELIVERY_CLEANUP_BATCH_SIZE", 500, minimum=1)
        if retry_max < retry_initial:
            raise ConfigurationError(
                "DELIVERY_RETRY_MAX_SECONDS must be greater than or equal to "
                "DELIVERY_RETRY_INITIAL_SECONDS"
            )
        smtp = _smtp_settings_from_env()
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
            delivery_history_retention_days=retention_days,
            delivery_cleanup_batch_size=cleanup_batch_size,
            smtp=smtp,
        )


def _smtp_settings_from_env() -> SmtpSettings | None:
    names = (
        "SMTP_HOST",
        "SMTP_FROM",
        "SMTP_PORT",
        "SMTP_SECURITY",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_TIMEOUT_SECONDS",
    )
    values = {name: os.getenv(name, "").strip() for name in names}
    host = values["SMTP_HOST"]
    if not host:
        if any(values[name] for name in names if name != "SMTP_HOST"):
            raise ConfigurationError("SMTP_HOST is required when SMTP is configured")
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        hostname_label = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
        if (
            len(host) > 253
            or not labels
            or any(not hostname_label.fullmatch(label) for label in labels)
        ):
            raise ConfigurationError("SMTP_HOST must be a valid hostname or IP address") from None
    sender = values["SMTP_FROM"]
    if not sender:
        raise ConfigurationError("SMTP_FROM is required when SMTP is configured")
    try:
        address = Address(addr_spec=sender)
    except (HeaderParseError, ValueError) as exc:
        raise ConfigurationError("SMTP_FROM must be a valid email address") from exc
    security = values["SMTP_SECURITY"].casefold() or "starttls"
    if security not in {"starttls", "ssl"}:
        raise ConfigurationError("SMTP_SECURITY must be starttls or ssl")
    default_port = 465 if security == "ssl" else 587
    raw_port = values["SMTP_PORT"] or str(default_port)
    raw_timeout = values["SMTP_TIMEOUT_SECONDS"] or "30"
    try:
        port = int(raw_port)
        timeout = int(raw_timeout)
    except ValueError as exc:
        raise ConfigurationError("SMTP_PORT and SMTP_TIMEOUT_SECONDS must be integers") from exc
    if not 1 <= port <= 65535:
        raise ConfigurationError("SMTP_PORT must be between 1 and 65535")
    if timeout < 1:
        raise ConfigurationError("SMTP_TIMEOUT_SECONDS must be at least 1")
    username = values["SMTP_USERNAME"] or None
    password = values["SMTP_PASSWORD"] or None
    if (username is None) != (password is None):
        raise ConfigurationError("SMTP_USERNAME and SMTP_PASSWORD must be set together")
    return SmtpSettings(
        host=host,
        port=port,
        sender=address.addr_spec,
        security=security,
        username=username,
        password=password,
        timeout_seconds=timeout,
    )
