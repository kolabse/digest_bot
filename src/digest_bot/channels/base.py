from __future__ import annotations

from typing import Protocol


class DeliveryChannel(Protocol):
    async def send(self, target: str, message: str) -> None: ...
