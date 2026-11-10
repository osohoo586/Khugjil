"""SPEC §3.10: no delivery without an active, versioned consent; revocation is immediate."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, time
from typing import Any

import pytest

from tests.fixtures.emails import Mailer
from tests.invariants.helpers import pending

pytestmark = pytest.mark.invariant(10)

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)  # 10:00 Asia/Ulaanbaatar, a Thursday
ACC, GRP, DEV, MEM, SHIFT = (uuid.UUID(int=i) for i in range(10, 15))


def snapshot(**member_kw: Any) -> Any:
    from khugjil.routing.model import (
        ConsentState,
        DeviceState,
        GroupState,
        MemberState,
        RoutingSnapshot,
        ShiftState,
    )

    member = MemberState(
        id=MEM,
        active=True,
        role="cashier",
        consents=(ConsentState(version=1, channel="web", revoked=False),),
        duty=(),
        telegram_linked=False,
    )
    member = replace(member, **member_kw)
    return RoutingSnapshot(
        min_consent_version=1,
        account_id=ACC,
        account_active=True,
        groups=(
            GroupState(
                id=GRP,
                active=True,
                account_ids=frozenset({ACC}),
                device_ids=frozenset({DEV}),
                member_ids=frozenset({MEM}),
            ),
        ),
        devices={
            DEV: DeviceState(
                id=DEV, active=True, kind="web", open_shift=ShiftState(id=SHIFT, member_id=MEM)
            )
        },
        members={MEM: member},
    )


def targets(snap: Any) -> list[Any]:
    from khugjil.routing.eligibility import eligible_targets

    return eligible_targets(snap, NOW)


@pending("routing")
def test_active_consent_delivers() -> None:
    assert [(t.kind, t.target_id) for t in targets(snapshot())] == [("device", DEV)]


@pending("routing")
def test_no_consent_no_delivery() -> None:
    assert targets(snapshot(consents=())) == []


@pending("routing")
def test_revoked_consent_no_delivery() -> None:
    from khugjil.routing.model import ConsentState

    revoked = (ConsentState(version=1, channel="web", revoked=True),)
    assert targets(snapshot(consents=revoked)) == []


@pending("routing")
def test_outdated_consent_version_no_delivery() -> None:
    snap = replace(snapshot(), min_consent_version=2)
    assert targets(snap) == []


@pending("routing")
def test_telegram_consent_does_not_cover_devices() -> None:
    from khugjil.routing.model import ConsentState

    only_tg = (ConsentState(version=1, channel="telegram", revoked=False),)
    assert targets(snapshot(consents=only_tg)) == []


@pending("routing")
def test_off_duty_by_schedule_no_delivery() -> None:
    from khugjil.routing.model import DutyWindow

    # Thursday is weekday 3; 10:00 local is outside 14:00-22:00.
    assert targets(snapshot(duty=(DutyWindow(weekday=3, start=time(14), end=time(22)),))) == []
    assert targets(snapshot(duty=(DutyWindow(weekday=3, start=time(9), end=time(22)),)))


@pending("delivery")
@pytest.mark.db
async def test_revocation_between_fanout_and_send_skips(world, mailer: Mailer) -> None:  # type: ignore[no-untyped-def]
    outcome = await world.ingest(mailer.bank_mail())
    await world.fanout(outcome.transaction_id)
    await world.revoke_consent(world.member_id)
    assert await world.send_all() == []
    statuses = {r["status"] for r in await world.rows("delivery")}
    assert statuses == {"skipped"}


@pending("consent service")
@pytest.mark.db
async def test_revocation_ends_shift_and_push_subscriptions(world) -> None:  # type: ignore[no-untyped-def]
    await world.add_push_subscription()
    await world.revoke_consent(world.member_id)
    shifts = await world.rows("shift")
    assert all(s["ended_at"] is not None for s in shifts)
    assert all(s["end_reason"] == "consent_revoked" for s in shifts)
    assert await world.count("push_subscription") == 0
