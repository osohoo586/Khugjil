"""Scheduled jobs (times in Asia/Ulaanbaatar). Each job iterates active tenants and works inside
that tenant's RLS context. Pure helpers take ``now`` for testability."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from arq import cron
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from khugjil.db.models import (
    Alert,
    DeviceCredential,
    DutyWindow,
    ForwardingConfirmation,
    IngestEvent,
    Invite,
    Member,
    PairingCode,
    PushSubscription,
    Session,
    Shift,
    TelegramLink,
    TelegramLinkCode,
    Tenant,
    Transaction,
)
from khugjil.db.tenancy import Resolver, SessionMaker, tenant_session
from khugjil.delivery.notifier import SendTarget
from khugjil.events import EventBus
from khugjil.ingest.pipeline import ALERT_FORWARDING_SILENT, ALERT_TEMPLATE_CHANGED
from khugjil.ingest.reasons import Reason
from khugjil.logs import get_logger
from khugjil.routing.model import DutyWindow as Window
from khugjil.routing.rules import on_duty
from khugjil.settings import BANK_TZ, Settings

_TZ = ZoneInfo(BANK_TZ)

ALERT_TEXT = {
    ALERT_FORWARDING_SILENT: (
        "Банкны мэдэгдлийн имэйл удаан хугацаанд ирээгүй байна. "
        "Gmail-ийн дамжуулалт болон шүүлтүүрээ шалгана уу."
    ),
    ALERT_TEMPLATE_CHANGED: (
        "Имэйлийн формат өөрчлөгдсөн байж магадгүй. "
        "Мэдэгдэл боловсруулагдаагүй тул админ хэсгийг шалгана уу."
    ),
}


def business_hours_between(start: datetime, end: datetime, hours: dict[str, Any]) -> float:
    """Business hours elapsed between two instants (tenant schedule, local time)."""
    if end <= start:
        return 0.0
    days = set(hours.get("days", range(7)))
    open_t = dtime.fromisoformat(hours.get("start", "09:00"))
    close_t = dtime.fromisoformat(hours.get("end", "21:00"))
    s_local, e_local = start.astimezone(_TZ), end.astimezone(_TZ)
    total = 0.0
    day: date = s_local.date()
    while day <= e_local.date() and (day - s_local.date()).days <= 31:
        if day.weekday() in days:
            win_start = datetime.combine(day, open_t, _TZ)
            win_end = datetime.combine(day, close_t, _TZ)
            lo, hi = max(win_start, s_local), min(win_end, e_local)
            if hi > lo:
                total += (hi - lo).total_seconds() / 3600
        day += timedelta(days=1)
    return total


async def health_check(sm: SessionMaker, now: datetime) -> list[uuid.UUID]:
    """Open a forwarding_silent alert when no bank email was accepted for N business hours."""
    opened: list[uuid.UUID] = []
    for tenant_id in await Resolver(sm).active_tenants():
        async with tenant_session(sm, tenant_id) as s:
            tenant = await s.get(Tenant, tenant_id)
            if tenant is None:  # pragma: no cover
                continue
            last = await s.scalar(
                select(func.max(IngestEvent.received_at)).where(
                    IngestEvent.reason_code == Reason.ACCEPTED.value
                )
            )
            since = last or tenant.created_at
            if (
                business_hours_between(since, now, tenant.business_hours)
                < tenant.health_silence_hours
            ):
                continue
            new = await s.scalar(
                insert(Alert)
                .values(tenant_id=tenant_id, kind=ALERT_FORWARDING_SILENT, opened_at=now)
                .on_conflict_do_nothing(
                    index_elements=["tenant_id", "kind"], index_where=Alert.resolved_at.is_(None)
                )
                .returning(Alert.id)
            )
            if new is not None:
                opened.append(tenant_id)
    return opened


async def auto_end_shifts(sm: SessionMaker, bus: EventBus, now: datetime) -> int:
    ended_total = 0
    local = now.astimezone(_TZ)
    for tenant_id in await Resolver(sm).active_tenants():
        events: list[tuple[uuid.UUID, uuid.UUID]] = []
        async with tenant_session(sm, tenant_id) as s:
            tenant = await s.get(Tenant, tenant_id)
            if tenant is None:  # pragma: no cover
                continue
            open_shifts = list(
                (await s.execute(select(Shift).where(Shift.ended_at.is_(None)))).scalars()
            )
            windows: dict[uuid.UUID, list[Window]] = {}
            for w in (await s.execute(select(DutyWindow))).scalars():
                windows.setdefault(w.member_id, []).append(
                    Window(w.weekday, w.start_local, w.end_local)
                )
            for sh in open_shifts:
                reason = None
                if sh.last_activity_at < now - timedelta(hours=tenant.shift_idle_hours):
                    reason = "inactivity"
                elif tenant.shift_end_time is not None:
                    end_today = datetime.combine(local.date(), tenant.shift_end_time, _TZ)
                    if sh.started_at < end_today <= now:
                        reason = "scheduled"
                if (
                    reason is None
                    and sh.member_id in windows
                    and not on_duty(windows[sh.member_id], now)
                ):
                    reason = "duty_end"
                if reason:
                    sh.ended_at, sh.end_reason = now, reason
                    events.append((sh.id, sh.device_id))
            if events:
                await s.execute(
                    delete(PushSubscription).where(
                        PushSubscription.shift_id.in_([e[0] for e in events])
                    )
                )
        for shift_id, device_id in events:
            await bus.publish(device_id, "shift_ended", shift_id=shift_id)
        ended_total += len(events)
    return ended_total


async def purge(sm: SessionMaker, settings: Settings, now: datetime) -> dict[str, int]:
    counts: dict[str, int] = {}

    def add(name: str, result: Any) -> None:
        counts[name] = counts.get(name, 0) + int(getattr(result, "rowcount", 0) or 0)

    for tenant_id in await Resolver(sm).active_tenants():
        async with tenant_session(sm, tenant_id) as s:
            add(
                "transaction",
                await s.execute(
                    delete(Transaction).where(
                        Transaction.received_at < now - timedelta(days=settings.retention_days)
                    )
                ),
            )
            add(
                "ingest_event",
                await s.execute(
                    delete(IngestEvent).where(
                        IngestEvent.received_at
                        < now - timedelta(days=settings.ingest_event_retention_days)
                    )
                ),
            )
            add(
                "forwarding_confirmation",
                await s.execute(
                    delete(ForwardingConfirmation).where(ForwardingConfirmation.expires_at < now)
                ),
            )
            add(
                "pairing_code",
                await s.execute(
                    delete(PairingCode).where(PairingCode.expires_at < now - timedelta(days=1))
                ),
            )
            add(
                "invite",
                await s.execute(delete(Invite).where(Invite.expires_at < now - timedelta(days=7))),
            )
            add("session", await s.execute(delete(Session).where(Session.expires_at < now)))
            add(
                "device_credential",
                await s.execute(delete(DeviceCredential).where(DeviceCredential.expires_at < now)),
            )
            add(
                "telegram_link_code",
                await s.execute(delete(TelegramLinkCode).where(TelegramLinkCode.expires_at < now)),
            )
            add(
                "alert",
                await s.execute(delete(Alert).where(Alert.resolved_at < now - timedelta(days=30))),
            )
    return counts  # fmt: skip


async def notify_alerts(sm: SessionMaker, telegram: Any, now: datetime) -> int:
    """Send each newly opened alert once to linked owners (no email content, ever)."""
    sent = 0
    for tenant_id in await Resolver(sm).active_tenants():
        async with tenant_session(sm, tenant_id) as s:
            alerts = list(
                (
                    await s.execute(
                        select(Alert).where(
                            Alert.resolved_at.is_(None), Alert.notified_at.is_(None)
                        )
                    )
                ).scalars()
            )
            owners = list(
                await s.scalars(
                    select(Member.id)
                    .join(TelegramLink, TelegramLink.member_id == Member.id)
                    .where(
                        Member.role == "owner",
                        Member.status == "active",
                        TelegramLink.status == "active",
                    )
                )
            )
            for alert in alerts:
                alert.notified_at = now
        for alert in alerts:
            for owner_id in owners:
                if telegram is not None:
                    await telegram.send_text(
                        SendTarget(tenant_id, "telegram", owner_id, owner_id, None),
                        ALERT_TEXT.get(
                            alert.kind, "Анхаарах асуудал гарлаа. Админ хэсгийг шалгана уу."
                        ),
                    )
                    sent += 1
    return sent


# ---------------------------------------------------------------- arq wrappers


async def _health_job(ctx: dict[str, Any]) -> int:
    return len(await health_check(ctx["rt"].sm, datetime.now(UTC)))


async def _shifts_job(ctx: dict[str, Any]) -> int:
    rt = ctx["rt"]
    return await auto_end_shifts(rt.sm, rt.bus, datetime.now(UTC))


async def _purge_job(ctx: dict[str, Any]) -> dict[str, int]:
    counts = await purge(ctx["rt"].sm, ctx["rt"].settings, datetime.now(UTC))
    get_logger().info("jobs.purge", count=sum(counts.values()))
    return counts


async def _alerts_job(ctx: dict[str, Any]) -> int:
    rt = ctx["rt"]
    return await notify_alerts(
        rt.sm, rt.delivery_deps().notifiers.get("telegram"), datetime.now(UTC)
    )


def cron_jobs() -> list[Any]:
    every_minute = set(range(60))
    return [
        cron(_shifts_job, minute=every_minute, run_at_startup=False),
        cron(_alerts_job, minute=every_minute),
        cron(_health_job, minute={0, 10, 20, 30, 40, 50}),
        cron(_purge_job, minute={7}),
    ]


__all__ = [
    "auto_end_shifts",
    "business_hours_between",
    "cron_jobs",
    "health_check",
    "notify_alerts",
    "purge",
]
