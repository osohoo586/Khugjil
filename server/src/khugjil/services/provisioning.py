"""Creating tenants, inbound addresses and bank accounts.

Used by the operator CLI, the admin panel and the test fixtures, so every path stores the same
encrypted values and tenant-scoped HMACs.
"""

from __future__ import annotations

import base64
import os
import unicodedata
import uuid
from dataclasses import dataclass
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


async def create_tenant(
    sm: SessionMaker, crypto: Crypto, name: str, *, pin_shifts: bool = True
) -> tuple[uuid.UUID, str]:
    """Returns (tenant_id, inbound local part)."""
    tenant_id = uuid.uuid4()
    async with tenant_session(sm, tenant_id) as s:
        s.add(Tenant(id=tenant_id, name=name, cashier_pin_shifts=pin_shifts))
        await s.flush()
        local = await _add_inbound(s, crypto, tenant_id)
    return tenant_id, local


MAX_ACTIVE_INBOUND = 5
LABEL_MAX = 60


class InboundLimitError(ValueError):
    """The tenant already has MAX_ACTIVE_INBOUND active addresses, or this is the last one."""


@dataclass(frozen=True)
class InboundView:
    id: uuid.UUID
    local_part: str
    label: str | None
    created_at: datetime


def _clean_label(label: str | None) -> str | None:
    """Owner-typed name: control, zero-width and bidi characters dropped, spaces collapsed."""
    text = "".join(c for c in (label or "") if unicodedata.category(c) not in ("Cc", "Cf"))
    value = " ".join(text.split())[:LABEL_MAX]
    return value or None


async def _new_inbound(
    s: AsyncSession, crypto: Crypto, tenant_id: uuid.UUID, label: str | None
) -> tuple[InboundAddress, str]:
    local = new_inbound_local_part()
    row = InboundAddress(
        tenant_id=tenant_id,
        local_part_hmac=inbound_hmac(crypto, local),
        local_part_enc=crypto.encrypt(
            local, field_aad(tenant_id, "inbound_address", "local_part_enc")
        ),
        label=_clean_label(label),
    )
    s.add(row)
    await s.flush()
    return row, local


async def _add_inbound(
    s: AsyncSession, crypto: Crypto, tenant_id: uuid.UUID, label: str | None = None
) -> str:
    return (await _new_inbound(s, crypto, tenant_id, label))[1]


async def _active_count(s: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Counts the active addresses while holding the tenant row lock until commit, so concurrent
    adds cannot pass the cap and concurrent retires cannot leave the tenant with none."""
    await s.execute(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
    return len(
        list(
            (
                await s.execute(select(InboundAddress.id).where(InboundAddress.status == "active"))
            ).scalars()
        )
    )


async def add_inbound(
    s: AsyncSession, crypto: Crypto, tenant_id: uuid.UUID, label: str | None
) -> InboundAddress:
    """One more active address beside the existing ones (each Gmail account can get its own).
    Returns the new row (its id goes to the audit log; the address itself never does)."""
    if await _active_count(s, tenant_id) >= MAX_ACTIVE_INBOUND:
        raise InboundLimitError("too many active inbound addresses")
    return (await _new_inbound(s, crypto, tenant_id, label))[0]


async def _active(s: AsyncSession, address_id: uuid.UUID) -> InboundAddress | None:
    """The tenant's own active address (RLS hides every other tenant's rows)."""
    row = await s.get(InboundAddress, address_id)
    return row if row is not None and row.status == "active" else None


async def retire_one_inbound(
    s: AsyncSession, tenant_id: uuid.UUID, address_id: uuid.UUID
) -> InboundAddress | None:
    """Retire one address immediately. Refuses the last active one: mail would stop silently."""
    remaining = await _active_count(s, tenant_id)  # locks first, then reads the row it changes
    row = await _active(s, address_id)
    if row is None:
        return None
    if remaining <= 1:
        raise InboundLimitError("the last active inbound address cannot be retired")
    row.status, row.retired_at = "retired", datetime.now(UTC)
    await s.flush()
    return row


async def replace_inbound(
    s: AsyncSession, crypto: Crypto, tenant_id: uuid.UUID, address_id: uuid.UUID
) -> InboundAddress | None:
    """Retire one (possibly leaked) address and issue a new one under the same name (PLAN Q28:
    the old one stops working at once; that Gmail's forwarding must be verified again)."""
    await _active_count(s, tenant_id)  # same lock: two replaces of one address issue one new one
    row = await _active(s, address_id)
    if row is None:
        return None
    row.status, row.retired_at = "retired", datetime.now(UTC)
    await s.flush()
    return (await _new_inbound(s, crypto, tenant_id, row.label))[0]


async def rename_inbound(
    s: AsyncSession, address_id: uuid.UUID, label: str | None
) -> InboundAddress | None:
    row = await _active(s, address_id)
    if row is not None:
        row.label = _clean_label(label)
        await s.flush()
    return row


async def list_inbound(s: AsyncSession, crypto: Crypto, tenant_id: uuid.UUID) -> list[InboundView]:
    rows = (
        await s.execute(
            select(InboundAddress)
            .where(InboundAddress.status == "active")
            .order_by(InboundAddress.created_at)
        )
    ).scalars()
    aad = field_aad(tenant_id, "inbound_address", "local_part_enc")
    return [
        InboundView(r.id, crypto.decrypt_str(r.local_part_enc, aad), r.label, r.created_at)
        for r in rows
    ]


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


def masked_label(number: str) -> str:
    """How to show a number the bank printed masked: "160*****04" -> "160…04"; a full number
    keeps the usual "••1234"."""
    if "*" not in number:
        return f"••{number[-4:]}"
    head, tail = number[: number.index("*")], number[number.rindex("*") + 1 :]
    return f"{head}…{tail}"


def account_suffix(crypto: Crypto, account: BankAccount) -> str:
    """The digits shown after "••": the last 4 of a full number, or what follows the mask."""
    number = crypto.decrypt_str(
        account.number_enc, field_aad(account.tenant_id, "bank_account", "number_enc")
    )
    return number[number.rindex("*") + 1 :] if "*" in number else number[-4:]
