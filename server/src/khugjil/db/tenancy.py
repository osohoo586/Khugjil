"""Tenant isolation at the session level.

All tenant data access goes through ``tenant_session``, which sets the transaction-local
``app.tenant_id`` that the RLS policies compare against. With no tenant set, policies match no
rows. The only cross-tenant reads are the ``Resolver`` methods, which call narrow SECURITY
DEFINER functions that take a hash/HMAC of a secret and return ids only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

SessionMaker = async_sessionmaker[AsyncSession]


def make_engine(url: str, *, pool_size: int = 10) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True, pool_size=pool_size, max_overflow=10)


def make_sessionmaker(engine: AsyncEngine) -> SessionMaker:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def tenant_session(sm: SessionMaker, tenant_id: UUID) -> AsyncIterator[AsyncSession]:
    """A session whose single transaction is bound to ``tenant_id`` (committed on success)."""
    if not isinstance(tenant_id, UUID):
        raise TypeError("tenant_id must be a UUID")
    async with sm() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
        )
        yield session


class Resolver:
    """Cross-tenant lookups. Each returns ids only, never tenant data."""

    def __init__(self, sm: SessionMaker) -> None:
        self._sm = sm

    async def _rows(self, sql: str, **params: Any) -> Sequence[Any]:
        async with self._sm() as session, session.begin():
            result = await session.execute(text(sql), params)
            return result.all()

    async def _first(self, sql: str, **params: Any) -> tuple[UUID, ...] | None:
        rows = await self._rows(sql, **params)
        return tuple(rows[0]) if len(rows) == 1 else None

    async def inbound(self, hmac: bytes) -> UUID | None:
        row = await self._first("SELECT khj_resolve_inbound(:h)", h=hmac)
        return row[0] if row and row[0] is not None else None

    async def device_token(self, token_hash: bytes) -> tuple[UUID, UUID] | None:
        row = await self._first("SELECT * FROM khj_resolve_device_token(:h)", h=token_hash)
        return (row[0], row[1]) if row else None

    async def session_token(self, token_hash: bytes) -> tuple[UUID, UUID, UUID] | None:
        row = await self._first("SELECT * FROM khj_resolve_session(:h)", h=token_hash)
        return (row[0], row[1], row[2]) if row else None

    async def login(self, login_name: str) -> tuple[UUID, UUID] | None:
        row = await self._first("SELECT * FROM khj_resolve_login(:n)", n=login_name)
        return (row[0], row[1]) if row else None

    async def pairing_token(self, token_hash: bytes) -> tuple[UUID, UUID] | None:
        row = await self._first("SELECT * FROM khj_resolve_pairing_token(:h)", h=token_hash)
        return (row[0], row[1]) if row else None

    async def pairing_short(self, code_hash: bytes) -> tuple[UUID, UUID] | None:
        row = await self._first("SELECT * FROM khj_resolve_pairing_short(:h)", h=code_hash)
        return (row[0], row[1]) if row else None

    async def invite(self, token_hash: bytes) -> tuple[UUID, UUID] | None:
        row = await self._first("SELECT * FROM khj_resolve_invite(:h)", h=token_hash)
        return (row[0], row[1]) if row else None

    async def telegram_chat(self, chat_hmac: bytes) -> tuple[UUID, UUID] | None:
        row = await self._first("SELECT * FROM khj_resolve_telegram_chat(:h)", h=chat_hmac)
        return (row[0], row[1]) if row else None

    async def telegram_code(self, code_hash: bytes) -> tuple[UUID, UUID] | None:
        row = await self._first("SELECT * FROM khj_resolve_telegram_code(:h)", h=code_hash)
        return (row[0], row[1]) if row else None

    async def active_tenants(self) -> list[UUID]:
        return [r[0] for r in await self._rows("SELECT * FROM khj_active_tenants()")]
