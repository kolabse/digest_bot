from __future__ import annotations

import logging

from .bot import build_application
from .config import ConfigurationError, Settings


class _SecretRedactingFormatter(logging.Formatter):
    def __init__(self, *args: object, secrets: tuple[str, ...], **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        for secret in self._secrets:
            rendered = rendered.replace(secret, "<redacted>")
        return rendered


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(level=logging.INFO)
    formatter = _SecretRedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        secrets=(settings.telegram_bot_token, settings.telegram_proxy_url or ""),
    )
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)
    # HTTP client INFO messages contain Telegram API URLs, which include the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    try:
        settings = Settings.from_env()
    except (ConfigurationError, ValueError) as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    _configure_logging(settings)
    build_application(settings).run_polling(allowed_updates=None)


if __name__ == "__main__":
    main()
