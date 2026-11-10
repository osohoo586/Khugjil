"""Database-backed test fixtures: a migrated throwaway database and per-test tenant "worlds".

Migrations run as the container superuser (owner role); the application connects as the
non-owner ``khugjil_app`` role, so row-level security applies exactly as in production.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from khugjil.crypto import Crypto
from khugjil.db.models import (
    Consent,
    Device,
    DeviceGroup,
    GroupAccount,
    GroupMember,
    Member,
    RecipientGroup,
    Shift,
)
from khugjil.db.tenancy import SessionMaker, make_engine, make_sessionmaker, tenant_session
from khugjil.ingest.dkim_arc import DnsFunc
from khugjil.ingest.pipeline import IngestDeps, Outcome, ingest
from khugjil.services.provisioning import add_bank_account, create_tenant
from khugjil.settings import Settings
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL
from tests.fixtures.emails import TENANT_ACCOUNT

SERVER_DIR = Path(__file__).resolve().parents[2]
APP_ROLE = "khugjil_app"
APP_PASSWORD = "test-app-only-not-secret"


@dataclass(frozen=True)
class TableInfo:
    name: str
    rls_enabled: bool
    rls_forced: bool
    policy_count: int


def _text(value: Any) -> str:
    if isinstance(value, bytes | memoryview):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


class TestDatabase:
    __test__ = False

    def __init__(
        self,
        settings: Settings,
        dnsfunc: DnsFunc,
        su: AsyncEngine,
        app_engine: AsyncEngine,
        redis: aioredis.Redis,
    ) -> None:
        self.settings = settings
        self.dnsfunc = dnsfunc
        self.su = su
        self.app_engine = app_engine
        self.sm: SessionMaker = make_sessionmaker(app_engine)
        self.redis = redis
        self.crypto = Crypto(settings.master_key_bytes)

    @classmethod
    async def start(cls, settings: Settings, dnsfunc: DnsFunc) -> TestDatabase:
        su = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        async with su.begin() as c:
            await c.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await c.execute(text("CREATE SCHEMA public"))
            await c.execute(
                text(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = "
                    f"'{APP_ROLE}') THEN CREATE ROLE {APP_ROLE} LOGIN PASSWORD "
                    f"'{APP_PASSWORD}' NOSUPERUSER NOBYPASSRLS; END IF; END $$"
                )
            )
        cfg = Config(str(SERVER_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(SERVER_DIR / "src/khugjil/db/migrations"))
        cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL.replace("%", "%%"))
        command.upgrade(cfg, "head")
        app_url = make_url(TEST_DATABASE_URL).set(username=APP_ROLE, password=APP_PASSWORD)
        app_engine = make_engine(app_url.render_as_string(hide_password=False), pool_size=5)
        redis = aioredis.from_url(TEST_REDIS_URL)
        return cls(settings, dnsfunc, su, app_engine, redis)

    async def close(self) -> None:
        await self.redis.aclose()
        await self.app_engine.dispose()
        await self.su.dispose()

    async def tables(self) -> list[str]:
        async with self.su.connect() as c:
            rows = await c.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            return sorted(r[0] for r in rows if r[0] != "alembic_version")

    async def reset(self) -> None:
        names = [t for t in await self.tables() if t != "consent_document"]
        async with self.su.begin() as c:
            await c.execute(text("TRUNCATE " + ", ".join(f'"{n}"' for n in names) + " CASCADE"))
        await self.redis.flushdb()

    def deps(self, enqueue: Callable[[uuid.UUID, uuid.UUID], Awaitable[None]]) -> IngestDeps:
        return IngestDeps(
            settings=self.settings,
            crypto=self.crypto,
            sessionmaker=self.sm,
            dnsfunc=self.dnsfunc,
            enqueue_fanout=enqueue,
        )

    # ---- introspection used by the isolation tests

    async def tenant_tables(self) -> list[TableInfo]:
        sql = """
        SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
               (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid)
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r' AND (c.relname = 'tenant' OR EXISTS (
          SELECT 1 FROM pg_attribute a
          WHERE a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped))
        """
        async with self.su.connect() as c:
            return [TableInfo(r[0], r[1], r[2], r[3]) for r in await c.execute(text(sql))]

    async def app_role_attributes(self) -> dict[str, bool]:
        async with self.su.connect() as c:
            row = (
                await c.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :r"),
                    {"r": APP_ROLE},
                )
            ).one()
        return {"superuser": row[0], "bypassrls": row[1]}

    async def app_rows_without_tenant(self, table: str) -> list[dict[str, Any]]:
        async with self.sm() as s, s.begin():
            return [dict(r) for r in (await s.execute(text(f'SELECT * FROM "{table}"'))).mappings()]

    async def link_group_account_raw(
        self, *, tenant_id: uuid.UUID, group_id: uuid.UUID, account_id: uuid.UUID
    ) -> None:
        async with tenant_session(self.sm, tenant_id) as s:
            s.add(GroupAccount(tenant_id=tenant_id, group_id=group_id, account_id=account_id))
            await s.flush()

    async def new_world(self, name: str) -> World:
        tenant_id, local = await create_tenant(self.sm, self.crypto, f"Тест {name} ХХК")
        async with tenant_session(self.sm, tenant_id) as s:
            account_id = await add_bank_account(
                s, self.crypto, tenant_id, TENANT_ACCOUNT, "Төв дэлгүүр"
            )
            group = RecipientGroup(tenant_id=tenant_id, name="Төв дэлгүүр")
            member = Member(
                tenant_id=tenant_id, display_name="Б. Сараа", role="cashier", status="active"
            )
            device = Device(tenant_id=tenant_id, name="Касс-1")
            s.add_all([group, member, device])
            await s.flush()
            s.add_all(
                [
                    GroupAccount(tenant_id=tenant_id, group_id=group.id, account_id=account_id),
                    GroupMember(tenant_id=tenant_id, group_id=group.id, member_id=member.id),
                    DeviceGroup(tenant_id=tenant_id, device_id=device.id, group_id=group.id),
                    Consent(tenant_id=tenant_id, member_id=member.id, version=1, channel="web"),
                ]
            )
            shift = Shift(tenant_id=tenant_id, device_id=device.id, member_id=member.id)
            s.add(shift)
            await s.flush()
        return World(
            db=self,
            tenant_id=tenant_id,
            inbound_address=f"{local}@{self.settings.inbound_domain}",
            account_id=account_id,
            group_id=group.id,
            member_id=member.id,
            device_id=device.id,
            shift_id=shift.id,
        )


@dataclass
class World:
    db: TestDatabase
    tenant_id: uuid.UUID
    inbound_address: str
    account_id: uuid.UUID
    group_id: uuid.UUID
    member_id: uuid.UUID
    device_id: uuid.UUID
    shift_id: uuid.UUID
    enqueued: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)

    async def _enqueue(self, tenant_id: uuid.UUID, txn_id: uuid.UUID) -> None:
        self.enqueued.append((tenant_id, txn_id))

    async def ingest(self, raw: bytes, rcpt: str | None = None) -> Outcome:
        return await ingest(self.db.deps(self._enqueue), rcpt or self.inbound_address, raw)

    async def count(self, table: str) -> int:
        async with self.db.su.connect() as c:
            return int((await c.execute(text(f'SELECT count(*) FROM "{table}"'))).scalar_one())

    async def rows(self, table: str) -> list[dict[str, Any]]:
        async with self.db.su.connect() as c:
            return [dict(r) for r in (await c.execute(text(f'SELECT * FROM "{table}"'))).mappings()]

    async def app_rows(self, table: str) -> list[dict[str, Any]]:
        async with tenant_session(self.db.sm, self.tenant_id) as s:
            return [dict(r) for r in (await s.execute(text(f'SELECT * FROM "{table}"'))).mappings()]

    async def dump_all_text(self) -> str:
        """Every row of every table as text; bytea values are decoded too, not just hex."""
        parts = []
        for table in await self.db.tables():
            for row in await self.rows(table):
                parts.append(json.dumps({k: _text(v) for k, v in row.items()}, ensure_ascii=False))
        return "\n".join(parts)

    async def redis_dump(self) -> str:
        r = self.db.redis
        out = []
        async for key in r.scan_iter():
            kind = (await r.type(key)).decode()
            if kind == "string":
                value: Any = await r.get(key)
            elif kind == "list":
                value = await r.lrange(key, 0, -1)
            elif kind == "hash":
                value = await r.hgetall(key)
            elif kind == "set":
                value = await r.smembers(key)
            elif kind == "zset":
                value = await r.zrange(key, 0, -1)
            else:
                value = await r.xrange(key) if kind == "stream" else kind
            out.append(f"{_text(key)}={_text(value)}")
        return "\n".join(out)

    async def add_account(self, number: str, alias: str = "Салбар") -> uuid.UUID:
        async with tenant_session(self.db.sm, self.tenant_id) as s:
            return await add_bank_account(s, self.db.crypto, self.tenant_id, number, alias)

    # Implemented with the delivery, consent and API steps.
    async def fanout(self, txn_id: uuid.UUID) -> list[uuid.UUID]:
        raise NotImplementedError

    async def send_all(self) -> list[Any]:
        raise NotImplementedError

    async def revoke_consent(self, member_id: uuid.UUID) -> None:
        raise NotImplementedError

    async def add_push_subscription(self) -> uuid.UUID:
        raise NotImplementedError

    async def api_transactions_json(self) -> str:
        raise NotImplementedError
