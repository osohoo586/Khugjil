"""Notifier interface (SPEC §6.3). Viber/SMS can be added as further implementations."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import ClassVar, Literal, Protocol
from uuid import UUID

from khugjil.delivery.format import Notification

Channel = Literal["sse", "webpush", "telegram"]


class SendResult(enum.Enum):
    SENT = "sent"
    RETRY = "retry"  # transient failure; the job is retried with backoff
    GONE = "gone"  # permanent: subscription expired, chat blocked the bot, ...


@dataclass(frozen=True)
class SendTarget:
    tenant_id: UUID
    channel: Channel
    target_id: UUID  # device id (sse/webpush) or member id (telegram)
    member_id: UUID
    shift_id: UUID | None


class Notifier(Protocol):
    channel: ClassVar[Channel]

    async def send(self, target: SendTarget, note: Notification) -> SendResult: ...
