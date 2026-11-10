"""Load a ``RoutingSnapshot`` for one bank account inside a tenant session."""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from khugjil.db.models import (
    BankAccount,
    Consent,
    Device,
    DeviceGroup,
    GroupAccount,
    GroupMember,
    Member,
    RecipientGroup,
    Shift,
    TelegramLink,
    Tenant,
)
from khugjil.db.models import DutyWindow as DutyRow
from khugjil.routing.model import (
    ConsentState,
    DeviceState,
    DutyWindow,
    GroupState,
    MemberState,
    RoutingSnapshot,
    ShiftState,
)


async def load_snapshot(s: AsyncSession, account_id: UUID) -> RoutingSnapshot | None:
    account = await s.get(BankAccount, account_id)
    if account is None:
        return None
    min_version = (await s.execute(select(Tenant.min_consent_version))).scalar_one()

    group_ids = list(
        await s.scalars(select(GroupAccount.group_id).where(GroupAccount.account_id == account_id))
    )
    groups = (
        await s.execute(select(RecipientGroup).where(RecipientGroup.id.in_(group_ids)))
    ).scalars()
    accounts_by_group: dict[UUID, set[UUID]] = defaultdict(set)
    devices_by_group: dict[UUID, set[UUID]] = defaultdict(set)
    members_by_group: dict[UUID, set[UUID]] = defaultdict(set)
    for gid, aid in (
        await s.execute(
            select(GroupAccount.group_id, GroupAccount.account_id).where(
                GroupAccount.group_id.in_(group_ids)
            )
        )
    ).all():
        accounts_by_group[gid].add(aid)
    for gid, did in (
        await s.execute(
            select(DeviceGroup.group_id, DeviceGroup.device_id).where(
                DeviceGroup.group_id.in_(group_ids)
            )
        )
    ).all():
        devices_by_group[gid].add(did)
    for gid, mid in (
        await s.execute(
            select(GroupMember.group_id, GroupMember.member_id).where(
                GroupMember.group_id.in_(group_ids)
            )
        )
    ).all():
        members_by_group[gid].add(mid)

    group_states = tuple(
        GroupState(
            id=g.id,
            active=g.status == "active",
            account_ids=frozenset(accounts_by_group[g.id]),
            device_ids=frozenset(devices_by_group[g.id]),
            member_ids=frozenset(members_by_group[g.id]),
        )
        for g in groups
    )

    device_ids = {d for ids in devices_by_group.values() for d in ids}
    shifts = {
        sh.device_id: sh
        for sh in (
            await s.execute(
                select(Shift).where(Shift.device_id.in_(device_ids), Shift.ended_at.is_(None))
            )
        ).scalars()
    }
    devices = {
        d.id: DeviceState(
            id=d.id,
            active=d.status == "active",
            kind="agent" if d.kind == "agent" else "web",
            open_shift=(
                ShiftState(id=shifts[d.id].id, member_id=shifts[d.id].member_id)
                if d.id in shifts
                else None
            ),
        )
        for d in (await s.execute(select(Device).where(Device.id.in_(device_ids)))).scalars()
    }

    member_ids = {m for ids in members_by_group.values() for m in ids} | {
        sh.member_id for sh in shifts.values()
    }
    consents: dict[UUID, list[ConsentState]] = defaultdict(list)
    for c in (await s.execute(select(Consent).where(Consent.member_id.in_(member_ids)))).scalars():
        consents[c.member_id].append(
            ConsentState(
                version=c.version,
                channel="telegram" if c.channel == "telegram" else "web",
                revoked=c.revoked_at is not None,
            )
        )
    duty: dict[UUID, list[DutyWindow]] = defaultdict(list)
    for w in (await s.execute(select(DutyRow).where(DutyRow.member_id.in_(member_ids)))).scalars():
        duty[w.member_id].append(DutyWindow(w.weekday, w.start_local, w.end_local))
    linked = set(
        await s.scalars(
            select(TelegramLink.member_id).where(
                TelegramLink.member_id.in_(member_ids), TelegramLink.status == "active"
            )
        )
    )
    members = {
        m.id: MemberState(
            id=m.id,
            active=m.status == "active",
            role=m.role,  # type: ignore[arg-type]
            consents=tuple(consents[m.id]),
            duty=tuple(duty[m.id]),
            telegram_linked=m.id in linked,
        )
        for m in (await s.execute(select(Member).where(Member.id.in_(member_ids)))).scalars()
    }

    return RoutingSnapshot(
        min_consent_version=min_version,
        account_id=account_id,
        account_active=account.status == "active",
        groups=group_states,
        devices=devices,
        members=members,
    )
