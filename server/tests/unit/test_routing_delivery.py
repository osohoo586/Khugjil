"""Routing rules, notification format and delivery service tests."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import text

from khugjil.db.models import Consent, DutyWindow, Member, TelegramLink
from khugjil.db.tenancy import tenant_session
from khugjil.delivery.format import build_notification, format_amount, format_datetime, format_time
from khugjil.delivery.notifier import SendResult, SendTarget
from khugjil.delivery.service import DeliveryDeps, ack, fanout, send
from khugjil.delivery.telegram import send_message_payload
from khugjil.events import RecordingEventBus, _event
from khugjil.routing.eligibility import eligible_targets
from khugjil.routing.model import (
    ConsentState,
    DeviceState,
    GroupState,
    MemberState,
    RoutingSnapshot,
    ShiftState,
)
from khugjil.routing.model import DutyWindow as Window
from khugjil.routing.rules import consent_active, on_duty
from khugjil.services.shifts import end_open_shifts
from tests.fixtures.emails import Mailer

VECTORS = json.loads(
    (Path(__file__).resolve().parents[3] / "shared" / "format_vectors.json").read_text()
)
NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)  # Thursday 10:00 local
U = [uuid.UUID(int=i) for i in range(100, 120)]
ACC, GRP, GRP2, DEV, DEV2, MEM, OWNER, SHIFT, OTHER = U[:9]
WEB = (ConsentState(1, "web", False),)
TG = (ConsentState(1, "telegram", False),)


def member(mid: uuid.UUID, **kw: Any) -> MemberState:
    base = MemberState(
        id=mid, active=True, role="cashier", consents=WEB, duty=(), telegram_linked=False
    )
    return replace(base, **kw)


def snap(**kw: Any) -> RoutingSnapshot:
    base = RoutingSnapshot(
        min_consent_version=1,
        account_id=ACC,
        account_active=True,
        groups=(
            GroupState(GRP, True, frozenset({ACC}), frozenset({DEV}), frozenset({MEM, OWNER})),
        ),
        devices={DEV: DeviceState(DEV, True, "web", ShiftState(SHIFT, MEM))},
        members={
            MEM: member(MEM),
            OWNER: member(OWNER, role="owner", consents=TG, telegram_linked=True),
        },
    )
    return replace(base, **kw)


def kinds(s: RoutingSnapshot, **kw: Any) -> list[tuple[str, uuid.UUID]]:
    return [(t.kind, t.target_id) for t in eligible_targets(s, NOW, **kw)]


# ---------------------------------------------------------------- eligibility


def test_device_and_owner_telegram() -> None:
    assert kinds(snap()) == [("device", DEV), ("telegram", OWNER)]


def test_inactive_account_or_group_or_unlinked_group() -> None:
    assert kinds(snap(account_active=False)) == []
    g = snap().groups[0]
    assert kinds(snap(groups=(replace(g, active=False),))) == []
    assert kinds(snap(groups=(replace(g, account_ids=frozenset({OTHER})),))) == []


def test_device_states() -> None:
    d = snap().devices[DEV]
    assert ("device", DEV) not in kinds(snap(devices={DEV: replace(d, active=False)}))
    assert ("device", DEV) not in kinds(snap(devices={DEV: replace(d, open_shift=None)}))
    assert ("device", DEV) not in kinds(snap(devices={}))
    agent = {DEV: replace(d, kind="agent")}
    assert ("device", DEV) not in kinds(snap(devices=agent))
    assert ("device", DEV) in kinds(snap(devices=agent), agent_enabled=True)


def test_shift_member_must_share_a_group_with_device() -> None:
    other = member(OTHER)
    devices = {DEV: DeviceState(DEV, True, "web", ShiftState(SHIFT, OTHER))}
    s = snap(devices=devices, members={**snap().members, OTHER: other})
    assert ("device", DEV) not in kinds(s)
    s2 = replace(
        s,
        groups=(
            *s.groups,
            GroupState(GRP2, True, frozenset({ACC}), frozenset({DEV}), frozenset({OTHER})),
        ),
    )
    assert ("device", DEV) in kinds(s2)


def test_member_states_for_device() -> None:
    m = snap().members[MEM]
    for changed in (replace(m, active=False), replace(m, consents=TG)):
        assert ("device", DEV) not in kinds(snap(members={MEM: changed}))
    s = snap(members={OWNER: snap().members[OWNER]})  # shift member missing entirely
    assert ("device", DEV) not in kinds(s)


def test_telegram_rules() -> None:
    o = snap().members[OWNER]
    for changed in (
        replace(o, role="admin"),
        replace(o, telegram_linked=False),
        replace(o, consents=WEB),
        replace(o, active=False),
        replace(o, duty=(Window(3, time(14), time(22)),)),
    ):
        assert ("telegram", OWNER) not in kinds(snap(members={**snap().members, OWNER: changed}))


def test_device_member_off_duty() -> None:
    m = replace(snap().members[MEM], duty=(Window(0, time(9), time(22)),))  # Monday only
    assert ("device", DEV) not in kinds(snap(members={**snap().members, MEM: m}))


@given(st.lists(st.booleans(), min_size=1, max_size=4))
def test_revoking_never_adds_targets(flags: list[bool]) -> None:
    consents = tuple(ConsentState(1, "web", f) for f in flags)
    s = snap(members={MEM: member(MEM, consents=consents)})
    assert len(eligible_targets(s, NOW)) <= len(eligible_targets(snap(), NOW))


def test_rules_helpers() -> None:
    assert consent_active([ConsentState(2, "web", False)], "web", 2)
    assert not consent_active([ConsentState(1, "web", False)], "web", 2)
    assert on_duty([], NOW)
    with pytest.raises(ValueError, match="timezone"):
        on_duty([Window(3, time(9), time(10))], datetime(2026, 10, 15, 10))
    # 09:59:59 local is outside a 10:00-11:00 window; 10:00 is inside; 11:00 is outside.
    w = [Window(3, time(10), time(11))]
    assert not on_duty(w, datetime(2026, 10, 15, 1, 59, 59, tzinfo=UTC))
    assert on_duty(w, datetime(2026, 10, 15, 2, 0, tzinfo=UTC))
    assert not on_duty(w, datetime(2026, 10, 15, 3, 0, tzinfo=UTC))


# ---------------------------------------------------------------- format


@pytest.mark.parametrize("v", VECTORS["amounts"], ids=lambda v: v["text"])
def test_shared_amount_vectors(v: dict[str, str]) -> None:
    assert format_amount(Decimal(v["amount"]), v["currency"]) == v["text"]


@pytest.mark.parametrize("v", VECTORS["times"], ids=lambda v: v["utc"])
def test_shared_time_vectors(v: dict[str, str]) -> None:
    at = datetime.fromisoformat(v["utc"].replace("Z", "+00:00"))
    assert format_time(at) == v["time"]
    assert format_datetime(at) == v["datetime"]


def test_notification_text_matches_spec_layout() -> None:
    n = build_notification(
        transaction_id=U[0],
        seq=1,
        amount=Decimal("2800.00"),
        currency="MNT",
        txn_at=datetime(2026, 10, 15, 0, 37, 22, tzinfo=UTC),
        description="Худалдан авалт",
        account_alias="Төв дэлгүүр",
        show_alias=True,
    )
    assert n.plain_text() == "Орлого\n2,800 ₮\nУтга: Худалдан авалт\n2026.10.15 08:37\nТөв дэлгүүр"
    hidden = replace(n, account_alias=None, description="", test=True)
    assert hidden.plain_text() == "Туршилт\nОрлого\n2,800 ₮\n2026.10.15 08:37"
    assert n.to_api()["amount_text"] == "2,800 ₮"
    payload = send_message_payload(chat_id=5, text="x" * 5000)
    assert len(payload["text"]) == 4096


def test_event_bus_rejects_unknown_events() -> None:
    with pytest.raises(ValueError, match="unknown event"):
        _event("raw_email")


# ---------------------------------------------------------------- delivery (database)


class Scripted:
    def __init__(self, result: SendResult) -> None:
        self.result = result
        self.calls: list[SendTarget] = []

    async def send(self, target: SendTarget, note: Any) -> SendResult:
        self.calls.append(target)
        return self.result


def deps_with(world: Any, result: SendResult) -> tuple[DeliveryDeps, Scripted]:
    n = Scripted(result)
    return DeliveryDeps(crypto=world.db.crypto, sessionmaker=world.db.sm, notifiers={
        "sse": n, "webpush": n, "telegram": n}), n  # fmt: skip


@pytest.mark.db
async def test_fanout_includes_webpush_and_owner_telegram(world: Any, mailer: Mailer) -> None:
    await world.add_push_subscription()
    async with tenant_session(world.db.sm, world.tenant_id) as s:
        owner = Member(
            tenant_id=world.tenant_id, display_name="Д. Болд", role="owner", status="active"
        )
        s.add(owner)
        await s.flush()
        await s.execute(
            text("INSERT INTO group_member (tenant_id, group_id, member_id) VALUES (:t, :g, :m)"),
            {"t": world.tenant_id, "g": world.group_id, "m": owner.id},
        )
        s.add(Consent(tenant_id=world.tenant_id, member_id=owner.id, version=1, channel="telegram"))
        s.add(
            TelegramLink(
                tenant_id=world.tenant_id, member_id=owner.id, chat_hmac=b"h", chat_enc=b"e"
            )
        )
        s.add(DutyWindow(tenant_id=world.tenant_id, member_id=owner.id, weekday=0,
                         start_local=time(0), end_local=time(23, 59)))  # fmt: skip
        for wd in range(1, 7):
            s.add(DutyWindow(tenant_id=world.tenant_id, member_id=owner.id, weekday=wd,
                             start_local=time(0), end_local=time(23, 59)))  # fmt: skip
    outcome = await world.ingest(mailer.bank_mail())
    await world.fanout(outcome.transaction_id)
    channels = sorted(r["channel"] for r in await world.rows("delivery"))
    assert channels == ["sse", "telegram", "webpush"]


@pytest.mark.db
async def test_send_retry_then_gone(world: Any, mailer: Mailer) -> None:
    outcome = await world.ingest(mailer.bank_mail())
    (delivery_id,) = await world.fanout(outcome.transaction_id)
    deps, n = deps_with(world, SendResult.RETRY)
    result, note = await send(deps, world.tenant_id, delivery_id)
    assert result is SendResult.RETRY
    assert note is not None
    (row,) = await world.rows("delivery")
    assert (row["status"], row["attempts"], row["last_error_code"]) == ("pending", 1, "retry")
    deps.notifiers["sse"].result = SendResult.GONE  # type: ignore[attr-defined]
    await send(deps, world.tenant_id, delivery_id)
    (row,) = await world.rows("delivery")
    assert (row["status"], row["attempts"]) == ("failed", 2)
    assert await send(deps, world.tenant_id, delivery_id) == (None, None)
    assert len(n.calls) == 2


@pytest.mark.db
async def test_ended_shift_skips_and_ack(world: Any, mailer: Mailer) -> None:
    outcome = await world.ingest(mailer.bank_mail())
    (delivery_id,) = await world.fanout(outcome.transaction_id)
    deps, _ = deps_with(world, SendResult.SENT)
    assert (await send(deps, world.tenant_id, delivery_id))[0] is SendResult.SENT
    assert await ack(deps, world.tenant_id, world.device_id, outcome.transaction_id)
    assert not await ack(deps, world.tenant_id, uuid.uuid4(), outcome.transaction_id)
    (row,) = await world.rows("delivery")
    assert row["status"] == "acked"

    async with tenant_session(world.db.sm, world.tenant_id) as s:
        ended = await end_open_shifts(
            s, reason="manual", now=datetime.now(UTC), device_id=world.device_id
        )
    assert [e[1] for e in ended] == [world.device_id]
    assert await fanout(deps, world.tenant_id, outcome.transaction_id) == []


@pytest.mark.db
async def test_end_open_shifts_guards(world: Any) -> None:
    async with tenant_session(world.db.sm, world.tenant_id) as s:
        with pytest.raises(ValueError, match="unknown end reason"):
            await end_open_shifts(s, reason="whatever", now=NOW, shift_id=world.shift_id)
        with pytest.raises(ValueError, match="refusing"):
            await end_open_shifts(s, reason="manual", now=NOW)


@pytest.mark.db
async def test_unknown_transaction_and_alias_display(world: Any, mailer: Mailer) -> None:
    assert await world.fanout(uuid.uuid4()) == []
    await world.add_account("3333333333", alias="Салбар-2")
    outcome = await world.ingest(mailer.bank_mail())
    await world.fanout(outcome.transaction_id)
    ((_, _, note),) = await world.send_all()
    assert note.account_alias == "Төв дэлгүүр"  # shown because the tenant now has 2 accounts


@pytest.mark.db
async def test_revoke_publishes_shift_ended(world: Any) -> None:
    bus = RecordingEventBus()
    from khugjil.services.consent import revoke_consent

    n = await revoke_consent(
        world.db.sm, bus, world.tenant_id, world.member_id, via="admin", now=NOW
    )
    assert n == 1
    assert bus.events == [
        (world.device_id, {"type": "shift_ended", "shift_id": str(world.shift_id)})
    ]
