"""Ending shifts. Every path that ends a shift also removes that shift's push subscriptions and
tells the device page to reset, so a member who left stops receiving anything immediately."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from khugjil.db.models import PushSubscription, Shift

END_REASONS = frozenset(
    {"manual", "inactivity", "scheduled", "duty_end", "member_revoked", "device_revoked",
     "consent_revoked"}
)  # fmt: skip


async def end_open_shifts(
    s: AsyncSession,
    *,
    reason: str,
    now: datetime,
    member_id: uuid.UUID | None = None,
    device_id: uuid.UUID | None = None,
    shift_id: uuid.UUID | None = None,
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """End matching open shifts; returns [(shift_id, device_id)] so callers can publish events."""
    if reason not in END_REASONS:
        raise ValueError("unknown end reason")
    if member_id is None and device_id is None and shift_id is None:
        raise ValueError("refusing to end every shift")
    stmt = update(Shift).where(Shift.ended_at.is_(None))
    if member_id is not None:
        stmt = stmt.where(Shift.member_id == member_id)
    if device_id is not None:
        stmt = stmt.where(Shift.device_id == device_id)
    if shift_id is not None:
        stmt = stmt.where(Shift.id == shift_id)
    rows = (
        await s.execute(
            stmt.values(ended_at=now, end_reason=reason).returning(Shift.id, Shift.device_id)
        )
    ).all()
    ended = [(r[0], r[1]) for r in rows]
    if ended:
        await s.execute(
            delete(PushSubscription).where(PushSubscription.shift_id.in_([e[0] for e in ended]))
        )
    return ended
