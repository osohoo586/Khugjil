"""Fan-out and send.

``fanout`` computes eligible targets and inserts one ``delivery`` row per (transaction, channel,
target) with ON CONFLICT DO NOTHING, so running it twice never duplicates anything.
``send`` claims a pending row under a row lock, RE-CHECKS eligibility right before sending (a
consent revoked or a shift ended in between yields ``skipped``), then hands the notification to
the channel's Notifier.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from khugjil.crypto import Crypto, field_aad
from khugjil.db.models import BankAccount, Delivery, PushSubscription, Transaction
from khugjil.db.tenancy import SessionMaker, tenant_session
from khugjil.delivery.format import Notification, build_notification
from khugjil.delivery.notifier import Channel, Notifier, SendResult, SendTarget
from khugjil.logs import get_logger
from khugjil.routing.eligibility import eligible_targets
from khugjil.routing.model import Target
from khugjil.routing.snapshot import load_snapshot


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class DeliveryDeps:
    crypto: Crypto
    sessionmaker: SessionMaker
    notifiers: Mapping[Channel, Notifier]
    agent_enabled: bool = False
    now: Callable[[], datetime] = field(default=_utcnow)


async def _targets(deps: DeliveryDeps, s: AsyncSession, txn: Transaction) -> list[Target]:
    if txn.bank_account_id is None:
        return []
    snap = await load_snapshot(s, txn.bank_account_id)
    if snap is None:
        return []
    return eligible_targets(snap, deps.now(), agent_enabled=deps.agent_enabled)


async def fanout(deps: DeliveryDeps, tenant_id: uuid.UUID, txn_id: uuid.UUID) -> list[uuid.UUID]:
    created: list[uuid.UUID] = []
    async with tenant_session(deps.sessionmaker, tenant_id) as s:
        txn = await s.get(Transaction, txn_id)
        if txn is None:
            return []
        for t in await _targets(deps, s, txn):
            channels: list[Channel]
            if t.kind == "telegram":
                channels = ["telegram"]
            else:
                has_push = await s.scalar(
                    select(func.count())
                    .select_from(PushSubscription)
                    .where(
                        PushSubscription.device_id == t.target_id,
                        PushSubscription.shift_id == t.shift_id,
                    )
                )
                channels = ["sse", "webpush"] if has_push else ["sse"]
            for channel in channels:
                new_id = await s.scalar(
                    insert(Delivery)
                    .values(
                        tenant_id=tenant_id,
                        transaction_id=txn_id,
                        channel=channel,
                        target_id=t.target_id,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["transaction_id", "channel", "target_id"]
                    )
                    .returning(Delivery.id)
                )
                if new_id is not None:
                    created.append(new_id)
    get_logger().info(
        "delivery.fanout", tenant_id=str(tenant_id), transaction_id=str(txn_id), count=len(created)
    )
    return created


async def notification_for(
    deps: DeliveryDeps, s: AsyncSession, tenant_id: uuid.UUID, txn: Transaction
) -> Notification:
    description = deps.crypto.decrypt_str(
        txn.description_enc, field_aad(tenant_id, "transaction", "description_enc")
    )
    alias = None
    if txn.bank_account_id is not None:
        account = await s.get(BankAccount, txn.bank_account_id)
        alias = account.alias if account else None
    account_count = await s.scalar(select(func.count()).select_from(BankAccount)) or 0
    return build_notification(
        transaction_id=txn.id,
        seq=txn.seq,
        amount=txn.amount,
        currency=txn.currency,
        txn_at=txn.txn_at,
        description=description,
        account_alias=alias,
        show_alias=account_count > 1,
    )


async def send(
    deps: DeliveryDeps, tenant_id: uuid.UUID, delivery_id: uuid.UUID
) -> tuple[SendResult | None, Notification | None]:
    """Returns (result, notification). (None, None) when the row was not pending."""
    log = get_logger()
    async with tenant_session(deps.sessionmaker, tenant_id) as s:
        d = await s.get(Delivery, delivery_id, with_for_update=True)
        if d is None or d.status != "pending":
            return None, None
        txn = await s.get(Transaction, d.transaction_id)
        targets = await _targets(deps, s, txn) if txn else []
        kind = "telegram" if d.channel == "telegram" else "device"
        match = next((t for t in targets if t.kind == kind and t.target_id == d.target_id), None)
        if txn is None or match is None:
            d.status = "skipped"
            log.info("delivery.skipped", tenant_id=str(tenant_id), delivery_id=str(d.id))
            return None, None
        note = await notification_for(deps, s, tenant_id, txn)
        d.status, d.sent_at, d.attempts = "sent", deps.now(), d.attempts + 1
        channel: Channel = d.channel  # type: ignore[assignment]

    target = SendTarget(tenant_id, channel, match.target_id, match.member_id, match.shift_id)
    result = await deps.notifiers[channel].send(target, note)
    if result is not SendResult.SENT:
        async with tenant_session(deps.sessionmaker, tenant_id) as s:
            await s.execute(
                update(Delivery)
                .where(Delivery.id == delivery_id)
                .values(
                    status="pending" if result is SendResult.RETRY else "failed",
                    sent_at=None,
                    last_error_code=result.value,
                )
            )
    log.info(
        "delivery.sent",
        tenant_id=str(tenant_id),
        delivery_id=str(delivery_id),
        channel=channel,
        status=result.value,
    )
    return result, note


async def ack(
    deps: DeliveryDeps, tenant_id: uuid.UUID, device_id: uuid.UUID, txn_id: uuid.UUID
) -> bool:
    async with tenant_session(deps.sessionmaker, tenant_id) as s:
        result = await s.execute(
            update(Delivery)
            .where(
                Delivery.transaction_id == txn_id,
                Delivery.channel == "sse",
                Delivery.target_id == device_id,
                Delivery.status.in_(["pending", "sent"]),
            )
            .values(status="acked", acked_at=deps.now())
        )
        return bool(getattr(result, "rowcount", 0))
