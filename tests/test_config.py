import pytest

from digest_bot.config import ConfigurationError, Settings


def test_retry_settings_have_safe_defaults(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.delenv("DELIVERY_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("DELIVERY_RETRY_INITIAL_SECONDS", raising=False)
    monkeypatch.delenv("DELIVERY_RETRY_MAX_SECONDS", raising=False)
    monkeypatch.delenv("DELIVERY_HISTORY_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("DELIVERY_CLEANUP_BATCH_SIZE", raising=False)

    settings = Settings.from_env()

    assert settings.delivery_max_attempts == 5
    assert settings.delivery_retry_initial_seconds == 60
    assert settings.delivery_retry_max_seconds == 3600
    assert settings.delivery_claim_lease_seconds == 3600
    assert settings.delivery_history_retention_days == 90
    assert settings.delivery_cleanup_batch_size == 500


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DELIVERY_MAX_ATTEMPTS", "0"),
        ("DELIVERY_RETRY_INITIAL_SECONDS", "0"),
        ("DELIVERY_RETRY_MAX_SECONDS", "invalid"),
        ("DELIVERY_CLAIM_LEASE_SECONDS", "59"),
        ("DELIVERY_HISTORY_RETENTION_DAYS", "0"),
        ("DELIVERY_CLEANUP_BATCH_SIZE", "0"),
    ],
)
def test_retry_settings_reject_invalid_values(monkeypatch, name: str, value: str) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigurationError, match=name):
        Settings.from_env()


def test_retry_maximum_must_not_be_less_than_initial(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DELIVERY_RETRY_INITIAL_SECONDS", "120")
    monkeypatch.setenv("DELIVERY_RETRY_MAX_SECONDS", "60")

    with pytest.raises(ConfigurationError, match="DELIVERY_RETRY_MAX_SECONDS"):
        Settings.from_env()
