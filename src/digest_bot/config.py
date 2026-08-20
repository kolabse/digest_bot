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


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    telegram_proxy_url: str | None
    gitlab_allowed_hosts: frozenset[str]
    database_path: Path
    admin_user_ids: frozenset[int]
    default_timezone: str
    scheduler_interval_seconds: int

    @classmethod
    def from_env(cls) -> Settings:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ConfigurationError("TELEGRAM_BOT_TOKEN is required")
        interval = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "30"))
        if interval < 10:
            raise ConfigurationError("SCHEDULER_INTERVAL_SECONDS must be at least 10")
        return cls(
            telegram_bot_token=token,
            telegram_proxy_url=os.getenv("TELEGRAM_PROXY_URL", "").strip() or None,
            gitlab_allowed_hosts=_parse_hosts(os.getenv("GITLAB_ALLOWED_HOSTS", "")),
            database_path=Path(os.getenv("DATABASE_PATH", "data/digest-bot.sqlite3")),
            admin_user_ids=_parse_ids(os.getenv("BOT_ADMIN_USER_IDS", "")),
            default_timezone=os.getenv("DEFAULT_TIMEZONE", "Asia/Yekaterinburg"),
            scheduler_interval_seconds=interval,
        )
