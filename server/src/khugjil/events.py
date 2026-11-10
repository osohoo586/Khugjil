"""Device event bus over Redis pub/sub.

Events carry ids only (never amounts or descriptions). The API process holding a device's SSE
stream re-loads and re-checks the transaction before writing anything to the browser.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

import redis.asyncio as aioredis

ALLOWED_EVENTS = frozenset({"txn", "shift_ended", "device_revoked", "settings_changed", "test"})


def device_channel(device_id: UUID) -> str:
    return f"khj:dev:{device_id}"


def _event(kind: str, **ids: UUID | str | None) -> dict[str, Any]:
    if kind not in ALLOWED_EVENTS:
        raise ValueError(f"unknown event {kind}")
    return {"type": kind, **{k: (str(v) if v is not None else None) for k, v in ids.items()}}


class EventBus(Protocol):
    async def publish(self, device_id: UUID, kind: str, **ids: UUID | str | None) -> None: ...


class RedisEventBus:
    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis

    async def publish(self, device_id: UUID, kind: str, **ids: UUID | str | None) -> None:
        await self._redis.publish(device_channel(device_id), json.dumps(_event(kind, **ids)))


@dataclass
class RecordingEventBus:
    events: list[tuple[UUID, dict[str, Any]]] = field(default_factory=list)

    async def publish(self, device_id: UUID, kind: str, **ids: UUID | str | None) -> None:
        self.events.append((device_id, _event(kind, **ids)))
