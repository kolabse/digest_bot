from __future__ import annotations

import logging
from datetime import UTC, datetime

from .channels.base import DeliveryChannel
from .digest import extract_digest, render_message
from .github import RepositoryContentsSource
from .schedule import digest_date_for_send_time, due_delivery
from .storage import Storage

LOGGER = logging.getLogger(__name__)


class DeliveryService:
    def __init__(
        self,
        storage: Storage,
        source: RepositoryContentsSource,
        channels: dict[str, DeliveryChannel],
    ) -> None:
        self._storage = storage
        self._source = source
        self._channels = channels

    async def dispatch_due(self, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        for subscription in self._storage.list_subscriptions():
            if not subscription.active or subscription.id is None:
                continue
            due = due_delivery(subscription.timezone, subscription.send_time, current)
            if due is None:
                continue
            digest_date = due.digest_date.isoformat()
            if not self._storage.claim_delivery(subscription.id, digest_date, current):
                continue
            try:
                markdown = await self._source.read(
                    subscription.repository,
                    subscription.digest_path,
                    subscription.ref,
                    subscription.token_env,
                )
                document = extract_digest(markdown, due.digest_date)
                message = render_message(
                    document,
                    digest_is_today=due.digest_date == due.local_now.date(),
                )
                channel = self._channels[subscription.channel]
                await channel.send(subscription.target, message)
                self._storage.complete_delivery(subscription.id, digest_date, datetime.now(UTC))
                LOGGER.info(
                    "Sent digest %s for subscription %s via %s",
                    digest_date,
                    subscription.id,
                    subscription.channel,
                )
            except Exception as exc:
                self._storage.release_delivery(subscription.id, digest_date, str(exc))
                LOGGER.exception("Delivery failed for subscription %s", subscription.id)

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
        )
