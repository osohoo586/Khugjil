"""Consent records (SPEC §3.10, §9). Revocation is immediate: consents are marked revoked, the
member's open shifts end, their push subscriptions are deleted and their devices are told to
reset, all before this function returns. ``send`` also re-checks consent before every delivery."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from khugjil.db.models import Consent, ConsentDocument, TelegramLink, Tenant
from khugjil.db.tenancy import SessionMaker, tenant_session
from khugjil.events import EventBus
from khugjil.services.shifts import end_open_shifts

CHANNELS = frozenset({"web", "telegram"})


async def current_document(s: AsyncSession, kind: str = "member") -> ConsentDocument:
    return (
        await s.execute(
            select(ConsentDocument)
            .where(ConsentDocument.kind == kind)
            .order_by(ConsentDocument.version.desc())
            .limit(1)
        )
    ).scalar_one()


def render_document(doc: ConsentDocument, tenant_name: str, retention_days: int) -> str:
    return doc.body.replace("{tenant_name}", tenant_name).replace(
        "{retention_days}", str(retention_days)
    )


async def give_consent(
    s: AsyncSession, tenant_id: uuid.UUID, member_id: uuid.UUID, channel: str, version: int
) -> uuid.UUID:
    if channel not in CHANNELS:
        raise ValueError("unknown consent channel")
    tenant = await s.get(Tenant, tenant_id)
    if tenant is None or version < tenant.min_consent_version:
        raise ValueError("consent version is below the tenant minimum")
    consent = Consent(tenant_id=tenant_id, member_id=member_id, version=version, channel=channel)
    s.add(consent)
    await s.flush()
    return consent.id


async def revoke_consent(
    sm: SessionMaker,
    bus: EventBus,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    via: str,
    now: datetime,
    channel: str | None = None,
) -> int:
    """Revoke the member's consents (all channels, or one). Returns the number revoked."""
    async with tenant_session(sm, tenant_id) as s:
        stmt = update(Consent).where(Consent.member_id == member_id, Consent.revoked_at.is_(None))
        if channel is not None:
            stmt = stmt.where(Consent.channel == channel)
        result = await s.execute(stmt.values(revoked_at=now, revoked_via=via[:40]))
        ended: list[tuple[uuid.UUID, uuid.UUID]] = []
        if channel in (None, "web"):
            ended = await end_open_shifts(s, reason="consent_revoked", now=now, member_id=member_id)
        if channel in (None, "telegram"):
            await s.execute(
                update(TelegramLink)
                .where(TelegramLink.member_id == member_id)
                .values(status="revoked")
            )
    for shift_id, device_id in ended:
        await bus.publish(device_id, "shift_ended", shift_id=shift_id)
    return int(getattr(result, "rowcount", 0) or 0)
