"""Admin sessions and CSRF tokens.

Opaque 256-bit tokens stored as SHA-256; idle timeout 30 min, absolute 12 h; TOTP step-up valid
for 5 min. The CSRF token is an HMAC of the session token (synchronizer pattern, no storage).
"""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete

from khugjil.crypto import Crypto, Purpose, constant_time_eq, new_token, token_hash
from khugjil.db.models import Session
from khugjil.db.tenancy import Resolver, SessionMaker, tenant_session

IDLE = timedelta(minutes=30)
ABSOLUTE = timedelta(hours=12)
STEPUP_VALID = timedelta(minutes=5)


def ip_prefix(ip: str | None) -> str | None:
    """Store only a /24 (IPv4) or /48 (IPv6) prefix."""
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    bits = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{addr}/{bits}", strict=False))


def csrf_token(crypto: Crypto, session_token: str) -> str:
    return crypto.hmac(Purpose.CSRF, session_token).hex()


def csrf_valid(crypto: Crypto, session_token: str, presented: str | None) -> bool:
    return bool(presented) and constant_time_eq(csrf_token(crypto, session_token), presented or "")


@dataclass(frozen=True)
class AdminSession:
    tenant_id: uuid.UUID
    member_id: uuid.UUID
    session_id: uuid.UUID
    token: str
    stepped_up: bool


async def create_session(
    sm: SessionMaker,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    now: datetime,
    ip: str | None,
    stepped_up: bool = True,
) -> str:
    token = new_token(32)
    async with tenant_session(sm, tenant_id) as s:
        s.add(
            Session(
                tenant_id=tenant_id,
                member_id=member_id,
                token_hash=token_hash(token),
                created_at=now,
                last_seen_at=now,
                expires_at=now + ABSOLUTE,
                stepup_at=now if stepped_up else None,
                ip_prefix=ip_prefix(ip),
            )
        )
    return token


async def load_session(sm: SessionMaker, token: str, now: datetime) -> AdminSession | None:
    found = await Resolver(sm).session_token(token_hash(token))
    if found is None:
        return None
    tenant_id, member_id, session_id = found
    async with tenant_session(sm, tenant_id) as s:
        row = await s.get(Session, session_id)
        if row is None or row.last_seen_at < now - IDLE:
            if row is not None:
                await s.delete(row)
            return None
        row.last_seen_at = now
        stepped = row.stepup_at is not None and row.stepup_at > now - STEPUP_VALID
    return AdminSession(tenant_id, member_id, session_id, token, stepped)


async def mark_stepped_up(sm: SessionMaker, sess: AdminSession, now: datetime) -> None:
    async with tenant_session(sm, sess.tenant_id) as s:
        row = await s.get(Session, sess.session_id)
        if row is not None:
            row.stepup_at = now


async def end_session(sm: SessionMaker, sess: AdminSession) -> None:
    async with tenant_session(sm, sess.tenant_id) as s:
        await s.execute(delete(Session).where(Session.id == sess.session_id))


async def end_member_sessions(sm: SessionMaker, tenant_id: uuid.UUID, member_id: uuid.UUID) -> None:
    async with tenant_session(sm, tenant_id) as s:
        await s.execute(delete(Session).where(Session.member_id == member_id))
