"""Who receives a transaction: one pure function over a ``RoutingSnapshot``.

Device target: the account is active; some active group contains the account, the device and
the member on the device's open shift; the device is active (and of an enabled kind); the member
is active, has an active web consent and is on duty.
Telegram target: the account is active; some active group contains the account and the owner;
the owner is active, linked, has an active Telegram consent and is on duty.
Off duty means no target at all, so nothing is queued (SPEC §6.5).
"""

from __future__ import annotations

from datetime import datetime

from khugjil.routing.model import RoutingSnapshot, Target
from khugjil.routing.rules import consent_active, on_duty


def eligible_targets(
    snap: RoutingSnapshot, now: datetime, *, agent_enabled: bool = False
) -> list[Target]:
    if not snap.account_active:
        return []
    groups = [g for g in snap.groups if g.active and snap.account_id in g.account_ids]
    targets: list[Target] = []

    for device_id in sorted({d for g in groups for d in g.device_ids}):
        device = snap.devices.get(device_id)
        if device is None or not device.active or device.open_shift is None:
            continue
        if device.kind == "agent" and not agent_enabled:
            continue
        member = snap.members.get(device.open_shift.member_id)
        if member is None or not member.active:
            continue
        if not any(device_id in g.device_ids and member.id in g.member_ids for g in groups):
            continue
        if not consent_active(member.consents, "web", snap.min_consent_version):
            continue
        if not on_duty(member.duty, now):
            continue
        targets.append(Target("device", device_id, member.id, device.open_shift.id))

    for member_id in sorted({m for g in groups for m in g.member_ids}):
        member = snap.members.get(member_id)
        if member is None or not member.active or member.role != "owner":
            continue
        if not member.telegram_linked:
            continue
        if not consent_active(member.consents, "telegram", snap.min_consent_version):
            continue
        if not on_duty(member.duty, now):
            continue
        targets.append(Target("telegram", member_id, member_id, None))

    return targets
