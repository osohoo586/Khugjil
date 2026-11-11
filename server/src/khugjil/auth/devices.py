"""Device pairing, device credentials and shifts started with a PIN."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from khugjil.auth.secrets import lockout_until, verify_secret
from khugjil.crypto import Crypto, Purpose, new_token, token_hash
from khugjil.db.models import (
    Consent,
    Device,
    DeviceCredential,
    DeviceGroup,
    GroupMember,
    Member,
    PairingCode,
    RecipientGroup,
    Shift,
    Tenant,
)
from khugjil.db.tenancy import Resolver, SessionMaker, tenant_session
from khugjil.routing.rules import consent_active
from khugjil.services.shifts import end_open_shifts

PAIRING_TTL = timedelta(minutes=10)
CREDENTIAL_TTL = timedelta(days=400)
CREDENTIAL_ROTATE = timedelta(days=30)
# After a rotation the old token keeps working this long, so a response lost in transit (or an
# agent that crashes before saving the new token) does not lock the device out.
ROTATION_GRACE = timedelta(minutes=5)
DEVICE_KINDS = frozenset({"web", "agent"})
SHIFT_ROLES = frozenset({"owner", "admin", "cashier"})
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class PairingError(Exception):
    pass


class ShiftError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def new_short_code() -> str:
    """12 Crockford base32 characters (60 bits), shown as XXXX-XXXX-XXXX."""
    value = int.from_bytes(os.urandom(8), "big") >> 4
    chars = "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in range(12))
    return f"{chars[:4]}-{chars[4:8]}-{chars[8:]}"


def normalize_short_code(code: str) -> str:
    table = str.maketrans({"I": "1", "L": "1", "O": "0", "U": "V"})
    return "".join(c for c in code.upper().translate(table) if c in _CROCKFORD)


def short_code_hash(crypto: Crypto, code: str) -> bytes:
    return crypto.hmac(Purpose.SHORT_CODE, normalize_short_code(code))


@dataclass(frozen=True)
class NewPairing:
    pairing_id: uuid.UUID
    token: str
    short_code: str
    expires_at: datetime


async def create_pairing(
    s: AsyncSession,
    crypto: Crypto,
    tenant_id: uuid.UUID,
    *,
    device_name: str,
    group_ids: list[uuid.UUID],
    created_by: uuid.UUID | None,
    now: datetime,
    kind: str = "web",
) -> NewPairing:
    if kind not in DEVICE_KINDS:
        raise PairingError("unknown device kind")
    if not group_ids:
        raise PairingError("at least one group is required")
    found = set(await s.scalars(select(RecipientGroup.id).where(RecipientGroup.id.in_(group_ids))))
    if found != set(group_ids):
        raise PairingError("unknown group")
    token, short = new_token(32), new_short_code()
    pairing = PairingCode(
        tenant_id=tenant_id,
        device_name=device_name.strip()[:60] or "Төхөөрөмж",
        kind=kind,
        group_ids=list(group_ids),
        token_hash=token_hash(token),
        short_code_hash=short_code_hash(crypto, short),
        expires_at=now + PAIRING_TTL,
        created_by=created_by,
    )
    s.add(pairing)
    await s.flush()
    return NewPairing(pairing.id, token, short, pairing.expires_at)


@dataclass(frozen=True)
class PairedDevice:
    tenant_id: uuid.UUID
    device_id: uuid.UUID
    token: str


async def _issue_credential(
    s: AsyncSession, tenant_id: uuid.UUID, device_id: uuid.UUID, now: datetime
) -> str:
    token = new_token(32)
    s.add(
        DeviceCredential(
            tenant_id=tenant_id,
            device_id=device_id,
            token_hash=token_hash(token),
            created_at=now,
            expires_at=now + CREDENTIAL_TTL,
        )
    )
    await s.flush()
    return token


async def redeem_pairing(
    sm: SessionMaker,
    crypto: Crypto,
    *,
    token: str | None,
    short_code: str | None,
    now: datetime,
    kind: str = "web",
) -> PairedDevice:
    resolver = Resolver(sm)
    if token:
        found = await resolver.pairing_token(token_hash(token))
    elif short_code:
        found = await resolver.pairing_short(short_code_hash(crypto, short_code))
    else:
        found = None
    if found is None:
        raise PairingError("invalid code")
    tenant_id, pairing_id = found
    async with tenant_session(sm, tenant_id) as s:
        pairing = await s.get(PairingCode, pairing_id, with_for_update=True)
        if pairing is None or pairing.used_at is not None or pairing.expires_at <= now:
            raise PairingError("expired or used")
        if pairing.kind != kind:
            # Same error as a wrong code, and the code stays usable by the right client.
            raise PairingError("wrong kind")
        pairing.used_at = now
        device = Device(tenant_id=tenant_id, name=pairing.device_name, kind=kind, paired_at=now)
        s.add(device)
        await s.flush()
        live_groups = set(
            await s.scalars(
                select(RecipientGroup.id).where(RecipientGroup.id.in_(pairing.group_ids))
            )
        )
        for gid in live_groups:
            s.add(DeviceGroup(tenant_id=tenant_id, device_id=device.id, group_id=gid))
        device_token = await _issue_credential(s, tenant_id, device.id, now)
    return PairedDevice(tenant_id, device.id, device_token)


@dataclass(frozen=True)
class DeviceAuth:
    tenant_id: uuid.UUID
    device_id: uuid.UUID
    rotated_token: str | None = None
    kind: str = "web"


async def authenticate_device(
    sm: SessionMaker,
    token: str,
    now: datetime,
    *,
    kind: str = "web",
    agent_version: str | None = None,
) -> DeviceAuth | None:
    """Resolve a device token. The token only counts for a device of the expected ``kind``: the
    web cookie never authenticates an agent, and an agent's bearer token never a web device."""
    h = token_hash(token)
    found = await Resolver(sm).device_token(h)
    if found is None:
        return None
    tenant_id, device_id = found
    rotated = None
    async with tenant_session(sm, tenant_id) as s:
        device = await s.get(Device, device_id)
        if device is None or device.kind != kind:
            return None
        cred = (
            await s.execute(select(DeviceCredential).where(DeviceCredential.token_hash == h))
        ).scalar_one()
        if device.last_seen_at is None or device.last_seen_at < now - timedelta(seconds=60):
            device.last_seen_at = now
        if kind == "agent" and agent_version and device.agent_version != agent_version:
            device.agent_version = agent_version
        if cred.created_at < now - CREDENTIAL_ROTATE and cred.expires_at > now + ROTATION_GRACE:
            rotated = await _issue_credential(s, tenant_id, device_id, now)
            cred.expires_at = now + ROTATION_GRACE
    return DeviceAuth(tenant_id, device_id, rotated, kind)


async def revoke_device(
    s: AsyncSession, device_id: uuid.UUID, *, by: uuid.UUID | None, now: datetime
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    device = await s.get(Device, device_id)
    if device is None:
        return []
    device.status, device.revoked_at, device.revoked_by = "revoked", now, by
    await s.execute(delete(DeviceCredential).where(DeviceCredential.device_id == device_id))
    return await end_open_shifts(s, reason="device_revoked", now=now, device_id=device_id)


# ---------------------------------------------------------------- shifts


async def shift_candidates(s: AsyncSession, device_id: uuid.UUID) -> list[Member]:
    """Members who share an active group with the device and may work a shift there."""
    rows = await s.execute(
        select(Member)
        .join(GroupMember, GroupMember.member_id == Member.id)
        .join(DeviceGroup, DeviceGroup.group_id == GroupMember.group_id)
        .join(RecipientGroup, RecipientGroup.id == GroupMember.group_id)
        .where(
            DeviceGroup.device_id == device_id,
            RecipientGroup.status == "active",
            Member.status == "active",
            Member.role.in_(SHIFT_ROLES),
            Member.pin_hash.is_not(None),
        )
        .order_by(Member.display_name)
        .distinct()
    )
    return list(rows.scalars())


async def start_shift(
    s: AsyncSession,
    tenant_id: uuid.UUID,
    device_id: uuid.UUID,
    member_id: uuid.UUID,
    pin: str,
    now: datetime,
) -> Shift:
    member = await s.get(Member, member_id, with_for_update=True)
    candidates = {m.id for m in await shift_candidates(s, device_id)}
    if member is None or member.id not in candidates:
        verify_secret(None, pin)
        raise ShiftError("not_allowed")
    if member.pin_locked_until is not None and member.pin_locked_until > now:
        raise ShiftError("locked")
    if not verify_secret(member.pin_hash, pin):
        raise ShiftError("wrong_pin")  # caller records the failure in its own transaction
    member.pin_failures, member.pin_locked_until = 0, None
    tenant = await s.get(Tenant, tenant_id)
    consents = (await s.execute(select(Consent).where(Consent.member_id == member_id))).scalars()
    from khugjil.routing.model import ConsentState

    states = [
        ConsentState(c.version, "web", c.revoked_at is not None)
        for c in consents
        if c.channel == "web"
    ]
    if tenant is None or not consent_active(states, "web", tenant.min_consent_version):
        raise ShiftError("consent_required")
    await end_open_shifts(s, reason="manual", now=now, device_id=device_id)
    shift = Shift(
        tenant_id=tenant_id, device_id=device_id, member_id=member_id, started_at=now,
        last_activity_at=now,
    )  # fmt: skip
    s.add(shift)
    await s.flush()
    return shift


async def record_pin_failure(
    sm: SessionMaker, tenant_id: uuid.UUID, member_id: uuid.UUID, now: datetime
) -> None:
    async with tenant_session(sm, tenant_id) as s:
        member = await s.get(Member, member_id, with_for_update=True)
        if member is not None:
            member.pin_failures += 1
            member.pin_locked_until = lockout_until(member.pin_failures, now)


async def open_shift(s: AsyncSession, device_id: uuid.UUID) -> Shift | None:
    return (
        await s.execute(select(Shift).where(Shift.device_id == device_id, Shift.ended_at.is_(None)))
    ).scalar_one_or_none()
