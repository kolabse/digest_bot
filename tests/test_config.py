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


def test_smtp_settings_are_optional_and_parse_secure_configuration(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    assert Settings.from_env().smtp is None

    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "digest@example.com")
    monkeypatch.setenv("SMTP_USERNAME", "digest-user")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("SMTP_SECURITY", "starttls")

    smtp = Settings.from_env().smtp

    assert smtp is not None
    assert smtp.host == "smtp.example.com"
    assert smtp.port == 587
    assert smtp.sender == "digest@example.com"
    assert smtp.security == "starttls"
    assert smtp.username == "digest-user"
    assert smtp.password == "secret"


@pytest.mark.parametrize(
    ("values", "match"),
    [
        ({"SMTP_HOST": "smtp.example.com"}, "SMTP_FROM"),
        (
            {
                "SMTP_HOST": "bad\nhost",
                "SMTP_FROM": "digest@example.com",
            },
            "SMTP_HOST",
        ),
        (
            {"SMTP_HOST": "smtp.example.com", "SMTP_FROM": "bad address"},
            "SMTP_FROM",
        ),
        (
            {
                "SMTP_HOST": "smtp.example.com",
                "SMTP_FROM": "digest@example.com",
                "SMTP_SECURITY": "plain",
            },
            "SMTP_SECURITY",
        ),
        (
            {
                "SMTP_HOST": "smtp.example.com",
                "SMTP_FROM": "digest@example.com",
                "SMTP_USERNAME": "user",
            },
            "SMTP_USERNAME.*SMTP_PASSWORD",
        ),
    ],
)
def test_smtp_settings_reject_incomplete_or_insecure_configuration(
    monkeypatch, values, match
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    for name in (
        "SMTP_HOST",
        "SMTP_FROM",
        "SMTP_PORT",
        "SMTP_SECURITY",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ConfigurationError, match=match):
        Settings.from_env()
