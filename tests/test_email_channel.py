import smtplib
from types import SimpleNamespace

import pytest

from digest_bot.config import SmtpSettings
from digest_bot.errors import PermanentDeliveryError, RetryableDeliveryError
from digest_bot.storage import Storage


class FakeSmtp:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.message = None
        self.to_addrs = None
        self.all_to_addrs: list[list[str]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def starttls(self, *, context) -> None:
        self.calls.append(("starttls", context is not None))

    def login(self, username: str, password: str) -> None:
        self.calls.append(("login", username, password))

    def send_message(self, message, *, from_addr: str, to_addrs: list[str]) -> None:
        self.calls.append(("send", from_addr))
        self.message = message
        self.to_addrs = to_addrs
        self.all_to_addrs.append(to_addrs)


async def test_email_channel_resolves_group_and_sends_starttls_message(tmp_path) -> None:
    from digest_bot.channels.email import EmailChannel

    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    group = storage.add_email_recipient_group(
        "123",
        42,
        "Команда",
        ["first@example.com", "second@example.com"],
    )
    smtp = FakeSmtp()
    settings = SmtpSettings(
        host="smtp.example.com",
        port=587,
        sender="digest@example.com",
        security="starttls",
        username="digest-user",
        password="secret",
        timeout_seconds=15,
    )
    channel = EmailChannel(storage, settings, smtp_factory=lambda **kwargs: smtp)

    await channel.send(storage.email_group_target(group.id), "<b>Новости</b> проекта")

    assert smtp.calls[:4] == [
        "ehlo",
        ("starttls", True),
        "ehlo",
        ("login", "digest-user", "secret"),
    ]
    assert smtp.all_to_addrs == [["first@example.com"], ["second@example.com"]]
    assert smtp.message["To"] == "undisclosed-recipients:;"
    assert smtp.message["Subject"] == "Ежедневный дайджест проекта"
    assert "Новости проекта" in smtp.message.get_body(preferencelist=("plain",)).get_content()
    assert "<b>Новости</b> проекта" in smtp.message.get_body(preferencelist=("html",)).get_content()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (smtplib.SMTPServerDisconnected("lost"), RetryableDeliveryError),
        (smtplib.SMTPDataError(451, b"later"), RetryableDeliveryError),
        (smtplib.SMTPAuthenticationError(535, b"bad auth"), PermanentDeliveryError),
        (smtplib.SMTPRecipientsRefused({"bad@example.com": (550, b"bad")}), PermanentDeliveryError),
    ],
)
async def test_email_channel_classifies_smtp_errors(tmp_path, error, expected) -> None:
    from digest_bot.channels.email import EmailChannel

    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    group = storage.add_email_recipient_group("123", 42, "Команда", ["first@example.com"])
    settings = SmtpSettings(
        host="smtp.example.com",
        port=465,
        sender="digest@example.com",
        security="ssl",
        username=None,
        password=None,
        timeout_seconds=15,
    )

    def fail_factory(**kwargs):
        raise error

    channel = EmailChannel(
        storage,
        settings,
        smtp_ssl_factory=fail_factory,
    )

    with pytest.raises(expected):
        await channel.send(storage.email_group_target(group.id), "Digest")


async def test_email_channel_rejects_missing_group(tmp_path) -> None:
    from digest_bot.channels.email import EmailChannel

    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    channel = EmailChannel(
        storage,
        SmtpSettings(
            host="smtp.example.com",
            port=465,
            sender="digest@example.com",
            security="ssl",
            username=None,
            password=None,
            timeout_seconds=15,
        ),
        smtp_ssl_factory=lambda **kwargs: SimpleNamespace(),
    )

    with pytest.raises(PermanentDeliveryError, match="получателей"):
        await channel.send("email-group:999", "Digest")


async def test_email_channel_retries_only_pending_recipients(tmp_path) -> None:
    from digest_bot.channels.email import EmailChannel

    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    group = storage.add_email_recipient_group(
        "123", 42, "Команда", ["first@example.com", "second@example.com"]
    )
    attempts: list[str] = []
    second_attempts = 0

    class PerRecipientSmtp(FakeSmtp):
        def send_message(self, message, *, from_addr: str, to_addrs: list[str]) -> None:
            nonlocal second_attempts
            recipient = to_addrs[0]
            attempts.append(recipient)
            if recipient == "second@example.com":
                second_attempts += 1
                if second_attempts == 1:
                    raise smtplib.SMTPDataError(451, b"later")

    settings = SmtpSettings(
        host="smtp.example.com",
        port=587,
        sender="digest@example.com",
        security="starttls",
        username=None,
        password=None,
        timeout_seconds=15,
    )
    channel = EmailChannel(
        storage,
        settings,
        smtp_factory=lambda **kwargs: PerRecipientSmtp(),
    )
    target = storage.email_group_target(group.id)

    with pytest.raises(RetryableDeliveryError):
        await channel.send(target, "<b>Digest</b>")
    await channel.send(target, "<b>Digest</b>")

    assert attempts == ["first@example.com", "second@example.com", "second@example.com"]


async def test_email_channel_classifies_message_construction_errors(tmp_path, monkeypatch) -> None:
    from digest_bot.channels import email as email_module
    from digest_bot.channels.email import EmailChannel

    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    group = storage.add_email_recipient_group("123", 42, "Команда", ["first@example.com"])
    channel = EmailChannel(
        storage,
        SmtpSettings(
            host="smtp.example.com",
            port=465,
            sender="digest@example.com",
            security="ssl",
            username=None,
            password=None,
            timeout_seconds=15,
        ),
        smtp_ssl_factory=lambda **kwargs: FakeSmtp(),
    )
    monkeypatch.setattr(
        email_module,
        "make_msgid",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("invalid domain")),
    )

    with pytest.raises(PermanentDeliveryError):
        await channel.send(storage.email_group_target(group.id), "Digest")
