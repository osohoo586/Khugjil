"""Member invitations (single use, 72 h). The token travels in the URL fragment and is redeemed
by POST, so it never reaches server logs or Referer headers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from khugjil.crypto import new_token, token_hash
from khugjil.db.models import Invite, Member, Tenant
from khugjil.db.tenancy import Resolver, SessionMaker, tenant_session
from khugjil.services.consent import current_document, render_document

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from khugjil.runtime import Runtime

INVITE_TTL = timedelta(hours=72)
LOGIN_ROLES = frozenset({"owner", "admin", "viewer"})


class InviteError(Exception):
    pass


async def create_invite(
    s: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    created_by: uuid.UUID | None,
    now: datetime,
) -> str:
    token = new_token(32)
    s.add(
        Invite(
            tenant_id=tenant_id,
            member_id=member_id,
            token_hash=token_hash(token),
            expires_at=now + INVITE_TTL,
            created_by=created_by,
            created_at=now,
        )
    )
    await s.flush()
    return token


def invite_link(settings_app_host: str, settings_admin_host: str, role: str, token: str) -> str:
    host = settings_admin_host if role in LOGIN_ROLES else settings_app_host
    return f"https://{host}/invite#t={token}"


async def create_owner_invite(
    rt: Runtime, tenant_id: uuid.UUID, display_name: str = "Эзэмшигч"
) -> str:
    from datetime import UTC

    now = datetime.now(UTC)
    async with tenant_session(rt.sm, tenant_id) as s:
        owner = Member(tenant_id=tenant_id, display_name=display_name, role="owner")
        s.add(owner)
        await s.flush()
        token = await create_invite(s, tenant_id, owner.id, created_by=None, now=now)
    return invite_link(rt.settings.app_host, rt.settings.admin_host, "owner", token)


@dataclass(frozen=True)
class InviteView:
    tenant_id: uuid.UUID
    invite_id: uuid.UUID
    member_id: uuid.UUID
    display_name: str
    role: str
    tenant_name: str
    consent_version: int
    consent_text: str


async def inspect_invite(
    sm: SessionMaker, token: str, now: datetime, retention_days: int
) -> InviteView:
    found = await Resolver(sm).invite(token_hash(token))
    if found is None:
        raise InviteError("invalid")
    tenant_id, invite_id = found
    async with tenant_session(sm, tenant_id) as s:
        invite = await s.get(Invite, invite_id)
        if invite is None or invite.used_at is not None or invite.expires_at <= now:
            raise InviteError("expired")
        member = await s.get(Member, invite.member_id)
        tenant = await s.get(Tenant, tenant_id)
        if member is None or tenant is None or member.status == "revoked":
            raise InviteError("invalid")
        doc = await current_document(s)
        return InviteView(
            tenant_id=tenant_id,
            invite_id=invite_id,
            member_id=member.id,
            display_name=member.display_name,
            role=member.role,
            tenant_name=tenant.name,
            consent_version=doc.version,
            consent_text=render_document(doc, tenant.name, retention_days),
        )


async def consume_invite(s: AsyncSession, invite_id: uuid.UUID, now: datetime) -> Invite:
    invite = await s.get(Invite, invite_id, with_for_update=True)
    if invite is None or invite.used_at is not None or invite.expires_at <= now:
        raise InviteError("expired")
    invite.used_at = now
    return invite
