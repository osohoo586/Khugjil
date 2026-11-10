"""Process-wide resources built from Settings (engine, Redis, notifiers, job queue)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx
import redis.asyncio as aioredis
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from sqlalchemy.ext.asyncio import AsyncEngine

from khugjil.crypto import Crypto
from khugjil.db.tenancy import Resolver, SessionMaker, make_engine, make_sessionmaker
from khugjil.delivery.channels import SseNotifier, TelegramNotifier, VapidConfig, WebPushNotifier
from khugjil.delivery.service import DeliveryDeps
from khugjil.events import RedisEventBus
from khugjil.ingest.dkim_arc import DnsFunc
from khugjil.ingest.pipeline import IngestDeps
from khugjil.settings import Settings


def arq_redis_settings(settings: Settings) -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url.get_secret_value())


@dataclass
class Runtime:
    settings: Settings
    crypto: Crypto
    engine: AsyncEngine
    sm: SessionMaker
    redis: aioredis.Redis
    arq: ArqRedis
    http: httpx.AsyncClient

    @classmethod
    async def create(cls, settings: Settings) -> Runtime:
        engine = make_engine(settings.database_url.get_secret_value())
        return cls(
            settings=settings,
            crypto=Crypto(settings.master_key_bytes),
            engine=engine,
            sm=make_sessionmaker(engine),
            redis=aioredis.from_url(settings.redis_url.get_secret_value()),  # type: ignore[no-untyped-call]
            arq=await create_pool(arq_redis_settings(settings)),
            http=httpx.AsyncClient(timeout=10, follow_redirects=False),
        )

    async def close(self) -> None:
        await self.http.aclose()
        await self.arq.aclose()
        await self.redis.aclose()
        await self.engine.dispose()

    @property
    def resolver(self) -> Resolver:
        return Resolver(self.sm)

    @property
    def bus(self) -> RedisEventBus:
        return RedisEventBus(self.redis)

    async def enqueue_fanout(self, tenant_id: uuid.UUID, txn_id: uuid.UUID) -> None:
        await self.arq.enqueue_job(
            "fanout_job", str(tenant_id), str(txn_id), _job_id=f"fanout:{txn_id}"
        )

    def ingest_deps(self, dnsfunc: DnsFunc) -> IngestDeps:
        return IngestDeps(
            settings=self.settings,
            crypto=self.crypto,
            sessionmaker=self.sm,
            dnsfunc=dnsfunc,
            enqueue_fanout=self.enqueue_fanout,
        )

    def delivery_deps(self) -> DeliveryDeps:
        s = self.settings
        vapid = (
            VapidConfig(s.vapid_private_key.get_secret_value(), s.vapid_subject)
            if s.vapid_private_key and s.vapid_subject
            else None
        )
        token = s.telegram_bot_token.get_secret_value() if s.telegram_bot_token else None
        return DeliveryDeps(
            crypto=self.crypto,
            sessionmaker=self.sm,
            notifiers={
                "sse": SseNotifier(self.bus),
                "webpush": WebPushNotifier(self.sm, self.crypto, vapid),
                "telegram": TelegramNotifier(self.sm, self.crypto, token, self.http),
            },
            agent_enabled=s.agent_devices_enabled,
        )
