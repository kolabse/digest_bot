from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Subscription:
    id: int | None
    channel: str
    target: str
    repository: str
    digest_path: str
    ref: str
    token_env: str | None
    timezone: str
    send_time: str
    created_by: int
    active: bool = True


@dataclass(frozen=True, slots=True)
class DigestDocument:
    date: str
    body: str | None
