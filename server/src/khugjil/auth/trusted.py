"""Remembered browsers ("do not ask for the code on this device", owner's request 2026-11-21).

After a successful TOTP sign-in the browser may receive a random 256-bit token (httpOnly,
Secure, SameSite=Strict, `__Host-` cookie). For TRUST_DAYS that browser signs in with the
password alone; the session it gets is NOT stepped up, so every sensitive action still asks for
the code. Only SHA-256 of the token is stored. Re-inviting or revoking the member forgets all of
their browsers (auth.sessions.end_member_sessions), and the member can forget any of them.
"""

from __future__ import annotations

import hmac
import re
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from khugjil.auth.sessions import ip_prefix
from khugjil.crypto import new_token, token_hash
from khugjil.db.models import TrustedDevice

TRUST_COOKIE = "__Host-khj_trust"
TRUST_DAYS = 30
TRUST_TTL = timedelta(days=TRUST_DAYS)
MAX_PER_MEMBER = 10

_BROWSERS = (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
             ("Chrome/", "Chrome"), ("Safari/", "Safari"))  # fmt: skip
_SYSTEMS = (("Windows", "Windows"), ("iPhone", "iPhone"), ("iPad", "iPad"),
            ("Android", "Android"), ("Mac OS X", "macOS"), ("Linux", "Linux"))  # fmt: skip


def device_label(user_agent: str | None) -> str:
    """A coarse name the member recognises ("Chrome, Windows"); the raw User-Agent is not kept."""
    ua = (user_agent or "")[:512]
    browser = next((name for marker, name in _BROWSERS if marker in ua), "Хөтөч")
    system = next((name for marker, name in _SYSTEMS if re.search(re.escape(marker), ua)), "")
    return f"{browser}, {system}" if system else browser


async def remember(
    s: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    now: datetime,
    user_agent: str | None,
    ip: str | None,
) -> tuple[str, TrustedDevice]:
    """Store a new remembered browser; returns the cookie value and the row. Keeps at most
    MAX_PER_MEMBER per member (the least recently used go first)."""
    token = new_token(32)
    row = TrustedDevice(
        tenant_id=tenant_id,
        member_id=member_id,
        token_hash=token_hash(token),
        label=device_label(user_agent),
        ip_prefix=ip_prefix(ip),
        created_at=now,
        last_used_at=now,
        expires_at=now + TRUST_TTL,
    )
    s.add(row)
    await s.flush()
    keep = select(TrustedDevice.id).where(
        TrustedDevice.member_id == member_id, TrustedDevice.expires_at > now
    )
    keep = keep.order_by(TrustedDevice.last_used_at.desc()).limit(MAX_PER_MEMBER)
    await s.execute(
        delete(TrustedDevice).where(
            TrustedDevice.member_id == member_id, TrustedDevice.id.not_in(keep)
        )
    )
    return token, row


async def recognise(
    s: AsyncSession, member_id: uuid.UUID, token: str | None, now: datetime
) -> TrustedDevice | None:
    """The remembered browser when this token belongs to this member and has not expired. The
    caller has already checked the password."""
    if not token or len(token) > 128:
        return None
    row = (
        await s.execute(
            select(TrustedDevice).where(
                TrustedDevice.token_hash == token_hash(token),
                TrustedDevice.member_id == member_id,
                TrustedDevice.expires_at > now,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    row.last_used_at = now
    return row


async def list_for(s: AsyncSession, member_id: uuid.UUID, now: datetime) -> list[TrustedDevice]:
    return list(
        (
            await s.execute(
                select(TrustedDevice)
                .where(TrustedDevice.member_id == member_id, TrustedDevice.expires_at > now)
                .order_by(TrustedDevice.last_used_at.desc())
            )
        ).scalars()
    )


async def forget(s: AsyncSession, member_id: uuid.UUID, device_id: uuid.UUID | None) -> int:
    """Forget one of the member's browsers, or all of them when device_id is None."""
    stmt = delete(TrustedDevice).where(TrustedDevice.member_id == member_id)
    if device_id is not None:
        stmt = stmt.where(TrustedDevice.id == device_id)
    result = await s.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)


def is_current(row: TrustedDevice, token: str | None) -> bool:
    if not token or len(token) > 128:
        return False
    return hmac.compare_digest(row.token_hash, token_hash(token))
