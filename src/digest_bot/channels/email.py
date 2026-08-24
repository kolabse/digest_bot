from __future__ import annotations

import asyncio
import hashlib
import smtplib
import ssl
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.errors import HeaderParseError
from email.message import EmailMessage
from email.utils import make_msgid
from html.parser import HTMLParser
from typing import Any

from ..config import SmtpSettings
from ..errors import PermanentDeliveryError, RetryableDeliveryError
from ..storage import Storage


class _PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        lines = [line.rstrip() for line in "".join(self.parts).splitlines()]
        return "\n".join(lines).strip()


def _plain_text(html_message: str) -> str:
    parser = _PlainTextParser()
    parser.feed(html_message)
    parser.close()
    return parser.text()


class EmailChannel:
    def __init__(
        self,
        storage: Storage,
        settings: SmtpSettings,
        *,
        smtp_factory: Callable[..., Any] = smtplib.SMTP,
        smtp_ssl_factory: Callable[..., Any] = smtplib.SMTP_SSL,
    ) -> None:
        self._storage = storage
        self._settings = settings
        self._smtp_factory = smtp_factory
        self._smtp_ssl_factory = smtp_ssl_factory
        self._recipient_lease = timedelta(seconds=max(600, settings.timeout_seconds * 8 + 60))

    async def send(
        self,
        target: str,
        message: str,
        *,
        delivery_key: str | None = None,
    ) -> None:
        try:
            group = self._storage.resolve_email_group_target(target)
        except ValueError as exc:
            raise PermanentDeliveryError("Некорректный адрес группы email.") from exc
        if group is None:
            raise PermanentDeliveryError("Группа получателей email не найдена.")
        identity = delivery_key or "direct"
        batch_key = hashlib.sha256((identity + "\0" + target + "\0" + message).encode()).hexdigest()
        claim_token, claimed, permanent_failure, in_flight = (
            self._storage.claim_email_delivery_batch(
                batch_key,
                target,
                group.recipients,
                datetime.now(UTC),
                stale_after=self._recipient_lease,
            )
        )
        retryable_failure = in_flight
        for recipient in claimed:
            if not self._storage.renew_email_delivery_claim(
                batch_key,
                claim_token,
                datetime.now(UTC),
                stale_after=self._recipient_lease,
            ):
                retryable_failure = True
                break
            try:
                await asyncio.to_thread(
                    self._send_sync,
                    recipient,
                    message,
                    batch_key,
                )
            except RetryableDeliveryError:
                self._storage.finish_email_delivery_recipient(
                    batch_key, recipient, claim_token, outcome="pending"
                )
                retryable_failure = True
            except PermanentDeliveryError:
                self._storage.finish_email_delivery_recipient(
                    batch_key, recipient, claim_token, outcome="failed"
                )
                permanent_failure = True
            else:
                if not self._storage.finish_email_delivery_recipient(
                    batch_key, recipient, claim_token, outcome="sent"
                ):
                    retryable_failure = True
        if retryable_failure:
            raise RetryableDeliveryError(
                "Временная ошибка доставки одному или нескольким email-получателям."
            )
        if permanent_failure:
            raise PermanentDeliveryError(
                "Один или несколько email-получателей отклонили сообщение."
            )

    def _send_sync(
        self,
        recipient: str,
        html_message: str,
        batch_key: str,
    ) -> None:
        try:
            email = EmailMessage()
            email["From"] = self._settings.sender
            email["To"] = "undisclosed-recipients:;"
            email["Subject"] = "Ежедневный дайджест проекта"
            email["Message-ID"] = make_msgid(idstring=batch_key[:32], domain=self._settings.host)
            email.set_content(_plain_text(html_message))
            email.add_alternative(html_message, subtype="html")
            context = ssl.create_default_context()
            if self._settings.security == "ssl":
                client_context = self._smtp_ssl_factory(
                    host=self._settings.host,
                    port=self._settings.port,
                    timeout=self._settings.timeout_seconds,
                    context=context,
                )
            else:
                client_context = self._smtp_factory(
                    host=self._settings.host,
                    port=self._settings.port,
                    timeout=self._settings.timeout_seconds,
                )
            with client_context as client:
                if self._settings.security == "starttls":
                    client.ehlo()
                    client.starttls(context=context)
                    client.ehlo()
                if self._settings.username is not None:
                    client.login(self._settings.username, self._settings.password or "")
                refused = client.send_message(
                    email,
                    from_addr=self._settings.sender,
                    to_addrs=[recipient],
                )
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
        except smtplib.SMTPRecipientsRefused as exc:
            codes = [int(response[0]) for response in exc.recipients.values()]
            if codes and all(400 <= code < 500 for code in codes):
                raise RetryableDeliveryError("Временный отказ SMTP-получателя.") from exc
            raise PermanentDeliveryError("SMTP отклонил получателя.") from exc
        except (
            smtplib.SMTPAuthenticationError,
            smtplib.SMTPNotSupportedError,
            smtplib.SMTPSenderRefused,
            ssl.SSLError,
        ) as exc:
            raise PermanentDeliveryError("SMTP отклонил отправку сообщения.") from exc
        except smtplib.SMTPResponseException as exc:
            if 400 <= exc.smtp_code < 500:
                raise RetryableDeliveryError("Временная ошибка SMTP.") from exc
            raise PermanentDeliveryError("SMTP отклонил отправку сообщения.") from exc
        except (smtplib.SMTPServerDisconnected, TimeoutError, OSError) as exc:
            raise RetryableDeliveryError("Не удалось подключиться к SMTP.") from exc
        except smtplib.SMTPException as exc:
            raise PermanentDeliveryError("Ошибка протокола SMTP.") from exc
        except (HeaderParseError, TypeError, ValueError) as exc:
            raise PermanentDeliveryError("Не удалось сформировать email-сообщение.") from exc
