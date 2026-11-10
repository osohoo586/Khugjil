"""Creating tenants, inbound addresses and bank accounts.

Used by the operator CLI, the admin panel and the test fixtures, so every path stores the same
encrypted values and tenant-scoped HMACs.
"""

from __future__ import annotations

import base64
import os
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from khugjil.crypto import Crypto, Purpose, field_aad
from khugjil.db.models import BankAccount, InboundAddress, Tenant
from khugjil.db.tenancy import SessionMaker, tenant_session
from khugjil.ingest.parsers.khan_v1 import canonical_account


def new_inbound_local_part() -> str:
    """24 lowercase base32 characters (120 bits)."""
    return base64.b32encode(os.urandom(15)).decode().lower()


def inbound_hmac(crypto: Crypto, local_part: str) -> bytes:
    return crypto.hmac(Purpose.INBOUND_HMAC, local_part.strip().lower())


def account_hmac(crypto: Crypto, tenant_id: uuid.UUID, number: str) -> bytes:
    return crypto.hmac(Purpose.ACCOUNT_HMAC, tenant_id, canonical_account(number))


async def create_tenant(sm: SessionMaker, crypto: Crypto, name: str) -> tuple[uuid.UUID, str]:
    """Returns (tenant_id, inbound local part)."""
    tenant_id = uuid.uuid4()
    async with tenant_session(sm, tenant_id) as s:
        s.add(Tenant(id=tenant_id, name=name))
        await s.flush()
        local = await _add_inbound(s, crypto, tenant_id)
    return tenant_id, local


async def _add_inbound(s: AsyncSession, crypto: Crypto, tenant_id: uuid.UUID) -> str:
    local = new_inbound_local_part()
    s.add(
        InboundAddress(
            tenant_id=tenant_id,
            local_part_hmac=inbound_hmac(crypto, local),
            local_part_enc=crypto.encrypt(
                local, field_aad(tenant_id, "inbound_address", "local_part_enc")
            ),
        )
    )
    await s.flush()
    return local


async def rotate_inbound(s: AsyncSession, crypto: Crypto, tenant_id: uuid.UUID) -> str:
    """Retire every active address immediately (PLAN Q28) and create a new one."""
    await s.execute(
        update(InboundAddress)
        .where(InboundAddress.status == "active")
        .values(status="retired", retired_at=datetime.now(UTC))
    )
    return await _add_inbound(s, crypto, tenant_id)


async def current_inbound(s: AsyncSession, crypto: Crypto, tenant_id: uuid.UUID) -> str | None:
    row = await s.scalar(
        select(InboundAddress.local_part_enc)
        .where(InboundAddress.status == "active")
        .order_by(InboundAddress.created_at.desc())
        .limit(1)
    )
    if row is None:
        return None
    return crypto.decrypt_str(row, field_aad(tenant_id, "inbound_address", "local_part_enc"))


async def add_bank_account(
    s: AsyncSession, crypto: Crypto, tenant_id: uuid.UUID, number: str, alias: str
) -> uuid.UUID:
    canon = canonical_account(number)
    if not canon.isdigit() or not 6 <= len(canon) <= 20:
        raise ValueError("account number must be 6-20 digits")
    account = BankAccount(
        tenant_id=tenant_id,
        number_enc=crypto.encrypt(canon, field_aad(tenant_id, "bank_account", "number_enc")),
        number_hmac=account_hmac(crypto, tenant_id, canon),
        alias=alias.strip()[:60],
    )
    s.add(account)
    await s.flush()
    return account.id


def account_suffix(crypto: Crypto, account: BankAccount) -> str:
    number = crypto.decrypt_str(
        account.number_enc, field_aad(account.tenant_id, "bank_account", "number_enc")
    )
    return number[-4:]
