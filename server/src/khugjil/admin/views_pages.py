"""Admin pages: overview, accounts, groups, email connection, history, settings, audit log."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import delete, func, select

from khugjil.admin.core import Manager, Owner, Reader, redirect, require_stepup, utcnow
from khugjil.crypto import field_aad
from khugjil.db.models import (
    Alert,
    AuditLog,
    BankAccount,
    Consent,
    Delivery,
    Device,
    DeviceGroup,
    ForwardingConfirmation,
    GroupAccount,
    GroupMember,
    IngestEvent,
    Member,
    RecipientGroup,
    Transaction,
)
from khugjil.delivery.format import build_notification
from khugjil.ingest.forwarding import CONFIRM_LINK
from khugjil.ingest.pipeline import ALERT_FORWARDING_SILENT, ALERT_TEMPLATE_CHANGED
from khugjil.ingest.reasons import Reason
from khugjil.services.provisioning import (
    account_suffix,
    add_bank_account,
    current_inbound,
    rotate_inbound,
)
from khugjil.settings import BANK_TZ

router = APIRouter()
_TZ = ZoneInfo(BANK_TZ)
ONLINE_WINDOW = timedelta(seconds=45)
PAGE_SIZE = 50
CSV_LIMIT = 10_000

REASON_LABELS = {
    Reason.ACCEPTED: "Хүлээн авсан",
    Reason.DUPLICATE: "Алгассан — давхардсан гүйлгээ",
    Reason.OWN_TRANSFER: "Алгассан — өөрийн данс хоорондын шилжүүлэг",
    Reason.NOT_TENANT_ACCOUNT: "Татгалзсан — бүртгэлгүй данс",
    Reason.ACCOUNT_INACTIVE: "Татгалзсан — идэвхгүй данс",
    Reason.FROM_NOT_ALLOWED: "Татгалзсан — илгээгч зөвшөөрөгдөөгүй",
    Reason.HEADER_DUPLICATE: "Татгалзсан — толгой мэдээлэл давхардсан",
    Reason.AUTH_FAIL: "Татгалзсан — DKIM баталгаажаагүй",
    Reason.ATTACHMENT: "Татгалзсан — хавсралттай",
    Reason.TOO_LARGE: "Татгалзсан — хэмжээ хэт том",
    Reason.MIME_INVALID: "Татгалзсан — бүтэц буруу",
    Reason.DENYLIST: "Татгалзсан — нууц код агуулсан байж болзошгүй",
    Reason.SUBJECT_MISMATCH: "Татгалзсан — гарчиг таарахгүй",
    Reason.TEMPLATE_MISMATCH: "Татгалзсан — формат таарахгүй",
    Reason.AMOUNT_MISMATCH: "Татгалзсан — дүн таарахгүй",
    Reason.NOT_INCOME: "Алгассан — зарлагын гүйлгээ",
    Reason.FWD_CONFIRM_STORED: "Gmail-ийн баталгаажуулах код ирсэн",
    Reason.FWD_CONFIRM_UNRECOGNIZED: "Татгалзсан — Gmail-ийн баталгаажуулах имэйл танигдсангүй",
    Reason.FWD_CONFIRM_AUTH_FAIL: "Татгалзсан — Gmail-ийн имэйлийн DKIM баталгаажаагүй",
}


def online(device: Device, now: datetime) -> bool:
    return device.last_seen_at is not None and device.last_seen_at > now - ONLINE_WINDOW


def local_day_start(now: datetime) -> datetime:
    return datetime.combine(now.astimezone(_TZ).date(), time(0), _TZ)


# ---------------------------------------------------------------- overview


@router.get("/", response_class=HTMLResponse)
async def overview(ctx: Reader) -> HTMLResponse:
    now = utcnow()
    async with ctx.session() as s:
        last_ok = await s.scalar(
            select(func.max(IngestEvent.received_at)).where(
                IngestEvent.reason_code == Reason.ACCEPTED.value
            )
        )
        devices = list((await s.execute(select(Device).where(Device.status == "active"))).scalars())
        failed = await s.scalar(
            select(func.count())
            .select_from(Delivery)
            .where(Delivery.status == "failed", Delivery.created_at > now - timedelta(hours=24))
        )
        count, total = (
            await s.execute(
                select(func.count(), func.coalesce(func.sum(Transaction.amount), 0)).where(
                    Transaction.txn_at >= local_day_start(now), Transaction.currency == "MNT"
                )
            )
        ).one()
        alerts = list((await s.execute(select(Alert).where(Alert.resolved_at.is_(None)))).scalars())
        pending = list(
            (
                await s.execute(
                    select(Member).where(Member.status == "invited").order_by(Member.display_name)
                )
            ).scalars()
        )
        consenting = set(
            await s.scalars(select(Consent.member_id).where(Consent.revoked_at.is_(None)))
        )
        cashiers_without_consent = list(
            (
                await s.execute(
                    select(Member).where(Member.status == "active", Member.role == "cashier")
                )
            ).scalars()
        )
        # Routing needs an account in a shared group: flag set-ups that silently get nothing.
        groups = list(
            (
                await s.execute(
                    select(RecipientGroup)
                    .where(RecipientGroup.status == "active")
                    .order_by(RecipientGroup.name)
                )
            ).scalars()
        )
        with_accounts = set(
            await s.scalars(
                select(GroupAccount.group_id)
                .join(BankAccount, BankAccount.id == GroupAccount.account_id)
                .where(BankAccount.status == "active")
            )
        )
        useful = [g.id for g in groups if g.id in with_accounts]
        members_reached = set(
            await s.scalars(select(GroupMember.member_id).where(GroupMember.group_id.in_(useful)))
        )
        devices_reached = set(
            await s.scalars(select(DeviceGroup.device_id).where(DeviceGroup.group_id.in_(useful)))
        )
    problems: list[tuple[str, str]] = []
    for a in alerts:
        if a.kind == ALERT_FORWARDING_SILENT:
            problems.append(("Банкны имэйл удаан хугацаанд ирээгүй байна.", "/email"))
        elif a.kind == ALERT_TEMPLATE_CHANGED:
            problems.append(("Имэйлийн формат өөрчлөгдсөн байж магадгүй.", "/email"))
    for d in devices:
        if d.last_seen_at is not None and d.last_seen_at < now - timedelta(hours=1):
            problems.append((f"{d.name} төхөөрөмж нэг цагаас дээш холбогдоогүй байна.", "/devices"))
    for m in pending:
        problems.append((f"{m.display_name} урилгаа хүлээн аваагүй байна.", "/members"))
    for m in cashiers_without_consent:
        if m.id not in consenting:
            problems.append(
                (f"{m.display_name} зөвшөөрөл өгөөгүй тул мэдэгдэл авахгүй.", "/members")
            )
    for g in groups:
        if g.id not in with_accounts:
            problems.append(
                (f"«{g.name}» бүлэгт данс холбогдоогүй тул гишүүд нь гүйлгээ харахгүй.", "/groups")
            )
    for m in cashiers_without_consent:  # all active cashiers
        if m.id not in members_reached:
            problems.append(
                (f"{m.display_name} данстай бүлэгт ороогүй тул гүйлгээ харахгүй.", "/members")
            )
    for d in devices:
        if d.id not in devices_reached:
            problems.append(
                (f"{d.name} төхөөрөмж данстай бүлэгт ороогүй тул гүйлгээ харахгүй.", "/devices")
            )
    if failed:
        problems.append((f"Сүүлийн 24 цагт {failed} мэдэгдэл илгээгдээгүй.", "/devices"))
    return ctx.render(
        "overview.html",
        last_ok=last_ok,
        forwarding_ok=not any(a.kind == ALERT_FORWARDING_SILENT for a in alerts),
        devices_online=sum(online(d, now) for d in devices),
        devices_total=len(devices),
        offline_names=[d.name for d in devices if not online(d, now)][:3],
        failed=failed or 0,
        today_count=count,
        today_total=Decimal(total),
        problems=problems,
    )


# ---------------------------------------------------------------- accounts


@router.get("/accounts", response_class=HTMLResponse)
async def accounts(ctx: Manager, error: str | None = None) -> HTMLResponse:
    async with ctx.session() as s:
        rows = list(
            (await s.execute(select(BankAccount).order_by(BankAccount.created_at))).scalars()
        )
        suffix = {a.id: account_suffix(ctx.rt.crypto, a) for a in rows}
    return ctx.render("accounts.html", accounts=rows, suffix=suffix, error=error)


@router.post("/accounts")
async def account_add(
    ctx: Manager,
    number: Annotated[str, Form(max_length=40)],
    alias: Annotated[str, Form(min_length=1, max_length=60)],
) -> Response:
    try:
        async with ctx.session() as s:
            account_id = await add_bank_account(s, ctx.rt.crypto, ctx.tenant_id, number, alias)
            await ctx.audit(
                s, "account.add", object_type="bank_account", object_id=account_id, label=alias
            )
    except ValueError:
        return await accounts(ctx, error="Дансны дугаар 6-20 оронтой тоо байх ёстой.")
    except Exception:  # unique violation: already registered
        return await accounts(ctx, error="Энэ данс бүртгэлтэй байна.")
    return redirect("/accounts")


@router.post("/accounts/{account_id}")
async def account_update(
    ctx: Manager,
    account_id: uuid.UUID,
    alias: Annotated[str, Form(min_length=1, max_length=60)],
    status: Annotated[str, Form(pattern="^(active|inactive)$")],
) -> Response:
    async with ctx.session() as s:
        account = await s.get(BankAccount, account_id)
        if account is None:
            raise HTTPException(404)
        account.alias, account.status = alias.strip(), status
        await ctx.audit(
            s, f"account.{status}", object_type="bank_account", object_id=account_id, label=alias
        )
    return redirect("/accounts")


# ---------------------------------------------------------------- groups


@router.get("/groups", response_class=HTMLResponse)
async def groups(ctx: Manager) -> HTMLResponse:
    async with ctx.session() as s:
        rows = list(
            (await s.execute(select(RecipientGroup).order_by(RecipientGroup.name))).scalars()
        )
        counts: dict[uuid.UUID, dict[str, int]] = {g.id: {"a": 0, "m": 0, "d": 0} for g in rows}
        for model, k in ((GroupAccount, "a"), (GroupMember, "m"), (DeviceGroup, "d")):
            for gid, n in (
                await s.execute(select(model.group_id, func.count()).group_by(model.group_id))
            ).all():
                counts[gid][k] = n
    return ctx.render("groups.html", groups=rows, counts=counts)


@router.post("/groups")
async def group_add(
    ctx: Manager, name: Annotated[str, Form(min_length=1, max_length=60)]
) -> Response:
    async with ctx.session() as s:
        group = RecipientGroup(tenant_id=ctx.tenant_id, name=name.strip())
        s.add(group)
        await s.flush()
        await ctx.audit(s, "group.add", object_type="group", object_id=group.id, label=group.name)
    return redirect(f"/groups/{group.id}")


@router.get("/groups/{group_id}", response_class=HTMLResponse)
async def group_edit(ctx: Manager, group_id: uuid.UUID) -> HTMLResponse:
    async with ctx.session() as s:
        group = await s.get(RecipientGroup, group_id)
        if group is None:
            raise HTTPException(404)
        accounts_ = list(
            (await s.execute(select(BankAccount).order_by(BankAccount.alias))).scalars()
        )
        members = list(
            (
                await s.execute(
                    select(Member).where(Member.status != "revoked").order_by(Member.display_name)
                )
            ).scalars()
        )
        devices = list(
            (
                await s.execute(
                    select(Device).where(Device.status == "active").order_by(Device.name)
                )
            ).scalars()
        )
        sel_a = set(
            await s.scalars(
                select(GroupAccount.account_id).where(GroupAccount.group_id == group_id)
            )
        )
        sel_m = set(
            await s.scalars(select(GroupMember.member_id).where(GroupMember.group_id == group_id))
        )
        sel_d = set(
            await s.scalars(select(DeviceGroup.device_id).where(DeviceGroup.group_id == group_id))
        )
    return ctx.render(
        "group_edit.html",
        group=group,
        accounts=accounts_,
        members=members,
        devices=devices,
        sel_a=sel_a,
        sel_m=sel_m,
        sel_d=sel_d,
    )


@router.post("/groups/{group_id}")
async def group_save(
    ctx: Manager,
    group_id: uuid.UUID,
    name: Annotated[str, Form(min_length=1, max_length=60)],
    status: Annotated[str, Form(pattern="^(active|inactive)$")],
    account_ids: Annotated[list[uuid.UUID], Form()] = [],  # noqa: B006 - FastAPI form default
    member_ids: Annotated[list[uuid.UUID], Form()] = [],  # noqa: B006
    device_ids: Annotated[list[uuid.UUID], Form()] = [],  # noqa: B006
) -> Response:
    async with ctx.session() as s:
        group = await s.get(RecipientGroup, group_id)
        if group is None:
            raise HTTPException(404)
        group.name, group.status = name.strip(), status
        # Only ids that exist in this tenant (RLS) can be linked; composite FKs enforce it too.
        valid_a = set(
            await s.scalars(select(BankAccount.id).where(BankAccount.id.in_(account_ids)))
        )
        valid_m = set(await s.scalars(select(Member.id).where(Member.id.in_(member_ids))))
        valid_d = set(await s.scalars(select(Device.id).where(Device.id.in_(device_ids))))
        if valid_a != set(account_ids) or valid_m != set(member_ids) or valid_d != set(device_ids):
            raise HTTPException(404)
        for model in (GroupAccount, GroupMember, DeviceGroup):
            await s.execute(delete(model).where(model.group_id == group_id))
        s.add_all(
            [
                GroupAccount(tenant_id=ctx.tenant_id, group_id=group_id, account_id=a)
                for a in valid_a
            ]
        )
        s.add_all(
            [GroupMember(tenant_id=ctx.tenant_id, group_id=group_id, member_id=m) for m in valid_m]
        )
        s.add_all(
            [DeviceGroup(tenant_id=ctx.tenant_id, group_id=group_id, device_id=d) for d in valid_d]
        )
        await ctx.audit(
            s, "group.update", object_type="group", object_id=group_id, label=group.name
        )
    return redirect("/groups")


# ---------------------------------------------------------------- email connection (owner only)


async def _email_state(ctx: Any) -> dict[str, Any]:
    now = utcnow()
    async with ctx.session() as s:
        address_local = await current_inbound(s, ctx.rt.crypto, ctx.tenant_id)
        fwd = (
            await s.execute(
                select(ForwardingConfirmation)
                .where(ForwardingConfirmation.expires_at > now)
                .order_by(ForwardingConfirmation.received_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        events = list(
            (
                await s.execute(
                    select(IngestEvent).order_by(IngestEvent.received_at.desc()).limit(20)
                )
            ).scalars()
        )
        alerts = {
            a.kind
            for a in (await s.execute(select(Alert).where(Alert.resolved_at.is_(None)))).scalars()
        }
    code = (
        ctx.rt.crypto.decrypt_str(
            fwd.code_enc, field_aad(ctx.tenant_id, "forwarding_confirmation", "code_enc")
        )
        if fwd
        else None
    )
    # Either a numeric code or (current Gmail) Google's confirmation link for the owner to open.
    link = code if code and CONFIRM_LINK.fullmatch(code) else None
    return {
        "address": f"{address_local}@{ctx.rt.settings.inbound_domain}" if address_local else None,
        "fwd_code": code if code and code.isdigit() else None,
        "fwd_link": link,
        "fwd_expires": fwd.expires_at if fwd else None,
        "events": [
            (e.received_at, REASON_LABELS.get(Reason(e.reason_code), e.reason_code)) for e in events
        ],
        "last_ok": next(
            (e.received_at for e in events if e.reason_code == Reason.ACCEPTED.value), None
        ),
        "alerts": alerts,
        "bank_senders": ctx.rt.settings.bank_senders,
    }


@router.get("/email", response_class=HTMLResponse)
async def email_page(ctx: Owner, notice: str | None = None) -> HTMLResponse:
    return ctx.render("email.html", notice=notice, **await _email_state(ctx))


@router.get("/email/status", response_class=HTMLResponse)
async def email_status(ctx: Owner) -> HTMLResponse:
    return ctx.render("_email_status.html", **await _email_state(ctx))


@router.post("/email/rotate")
async def email_rotate(ctx: Owner) -> Response:
    require_stepup(ctx)
    async with ctx.session() as s:
        await rotate_inbound(s, ctx.rt.crypto, ctx.tenant_id)
        await ctx.audit(s, "inbound.rotate", object_type="inbound_address")
    return redirect("/email")


@router.post("/email/test")
async def email_test(ctx: Owner) -> Response:
    """Send a clearly labelled test notification through the live channels (no transaction)."""
    now = utcnow()
    note = build_notification(
        transaction_id=uuid.UUID(int=0),
        seq=0,
        amount=Decimal("100.00"),
        currency="MNT",
        txn_at=now,
        description="Туршилтын мэдэгдэл",
        account_alias=None,
        show_alias=False,
        test=True,
    )
    async with ctx.session() as s:
        device_ids = list(
            await s.scalars(
                select(Device.id)
                .where(Device.status == "active")
                .where(Device.id.in_(select(func.distinct(DeviceGroup.device_id))))
            )
        )
        await ctx.audit(s, "notification.test")
    for device_id in device_ids:
        await ctx.rt.bus.publish(device_id, "test")
    telegram = ctx.rt.delivery_deps().notifiers.get("telegram")
    if telegram is not None and hasattr(telegram, "send_text"):
        from khugjil.delivery.notifier import SendTarget

        await telegram.send_text(
            SendTarget(ctx.tenant_id, "telegram", ctx.member.id, ctx.member.id, None),
            note.plain_text(),
        )
    return redirect("/email?notice=test")


# ---------------------------------------------------------------- history + CSV


def _parse_filters(
    date_from: str | None, date_to: str | None, amount_min: str | None, amount_max: str | None
) -> tuple[datetime | None, datetime | None, Decimal | None, Decimal | None]:
    def d(v: str | None, end: bool) -> datetime | None:
        if not v:
            return None
        try:
            day = date.fromisoformat(v.replace(".", "-"))
        except ValueError:
            return None
        return datetime.combine(day + timedelta(days=1) if end else day, time(0), _TZ)

    def a(v: str | None) -> Decimal | None:
        if not v:
            return None
        try:
            value = Decimal(v.replace(",", "").strip())
        except InvalidOperation:
            return None
        return value if value.is_finite() else None

    return d(date_from, False), d(date_to, True), a(amount_min), a(amount_max)


async def _history_rows(
    ctx: Any, filters: tuple[Any, ...], account_id: uuid.UUID | None, limit: int, offset: int
) -> tuple[list[dict[str, Any]], int]:
    start, end, lo, hi = filters
    conditions = []
    if start is not None:
        conditions.append(Transaction.txn_at >= start)
    if end is not None:
        conditions.append(Transaction.txn_at < end)
    if lo is not None:
        conditions.append(Transaction.amount >= lo)
    if hi is not None:
        conditions.append(Transaction.amount <= hi)
    if account_id is not None:
        conditions.append(Transaction.bank_account_id == account_id)
    async with ctx.session() as s:
        total = (
            await s.scalar(select(func.count()).select_from(Transaction).where(*conditions)) or 0
        )
        txns = list(
            (
                await s.execute(
                    select(Transaction)
                    .where(*conditions)
                    .order_by(Transaction.txn_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).scalars()
        )
        accounts_ = {a.id: a for a in (await s.execute(select(BankAccount))).scalars()}
        suffix = {a.id: account_suffix(ctx.rt.crypto, a) for a in accounts_.values()}
    aad = field_aad(ctx.tenant_id, "transaction", "description_enc")
    rows = []
    for t in txns:
        acct = accounts_.get(t.bank_account_id) if t.bank_account_id else None
        rows.append(
            {
                "txn_at": t.txn_at,
                "account": f"{acct.alias} ••{suffix[acct.id]}" if acct else "—",
                "amount": t.amount,
                "currency": t.currency,
                "description": ctx.rt.crypto.decrypt_str(t.description_enc, aad),
            }
        )
    return rows, int(total)


@router.get("/history", response_class=HTMLResponse)
async def history(
    ctx: Reader,
    date_from: str | None = None,
    date_to: str | None = None,
    account: str | None = None,
    amount_min: str | None = None,
    amount_max: str | None = None,
    page: int = 1,
) -> HTMLResponse:
    page = max(1, min(page, 10_000))
    account_id = uuid.UUID(account) if account and len(account) == 36 else None
    filters = _parse_filters(date_from, date_to, amount_min, amount_max)
    rows, total = await _history_rows(ctx, filters, account_id, PAGE_SIZE, (page - 1) * PAGE_SIZE)
    async with ctx.session() as s:
        accounts_ = list(
            (await s.execute(select(BankAccount).order_by(BankAccount.alias))).scalars()
        )
    return ctx.render(
        "history.html",
        rows=rows,
        total=total,
        page=page,
        pages=max(1, -(-total // PAGE_SIZE)),
        accounts=accounts_,
        f={
            "date_from": date_from or "",
            "date_to": date_to or "",
            "account": account or "",
            "amount_min": amount_min or "",
            "amount_max": amount_max or "",
        },
    )


def csv_safe(value: str) -> str:
    """Neutralise spreadsheet formula injection in payer-written text."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


@router.get("/history.csv")
async def history_csv(
    ctx: Manager,
    date_from: str | None = None,
    date_to: str | None = None,
    account: str | None = None,
    amount_min: str | None = None,
    amount_max: str | None = None,
) -> Response:
    require_stepup(ctx)
    account_id = uuid.UUID(account) if account and len(account) == 36 else None
    filters = _parse_filters(date_from, date_to, amount_min, amount_max)
    rows, _ = await _history_rows(ctx, filters, account_id, CSV_LIMIT, 0)
    async with ctx.session() as s:
        await ctx.audit(
            s,
            "history.export_csv",
            meta={
                "rows": len(rows),
                "filters": {
                    "date_from": date_from,
                    "date_to": date_to,
                    "amount_min": amount_min,
                    "amount_max": amount_max,
                },
            },
        )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Огноо", "Данс", "Дүн", "Валют", "Утга"])
    for r in rows:
        writer.writerow(
            [
                r["txn_at"].astimezone(_TZ).strftime("%Y.%m.%d %H:%M"),
                csv_safe(r["account"]),
                str(r["amount"]),
                r["currency"],
                csv_safe(r["description"]),
            ]
        )
    return Response(
        "﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": 'attachment; filename="guilgee.csv"'},
    )


# ---------------------------------------------------------------- settings


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(ctx: Manager, saved: int = 0, error: str | None = None) -> HTMLResponse:
    return ctx.render("settings.html", t=ctx.tenant, saved=bool(saved), error=error)


@router.post("/settings")
async def settings_save(
    ctx: Manager,
    accent: Annotated[str, Form(pattern="^(green|navy)$")],
    toast_seconds: Annotated[int, Form(ge=5, le=20)],
    health_silence_hours: Annotated[int, Form(ge=1, le=72)],
    shift_idle_hours: Annotated[int, Form(ge=1, le=24)],
    business_start: Annotated[str, Form(pattern=r"^\d{2}:\d{2}$")],
    business_end: Annotated[str, Form(pattern=r"^\d{2}:\d{2}$")],
    business_days: Annotated[list[int], Form()] = [],  # noqa: B006
    shift_end_time: Annotated[str, Form(pattern=r"^(\d{2}:\d{2})?$")] = "",
    voice_max_amount: Annotated[str, Form(max_length=20)] = "",
    voice_enabled: Annotated[bool, Form()] = False,
    show_today_totals: Annotated[bool, Form()] = False,
    include_own_transfers: Annotated[bool, Form()] = False,
) -> Response:
    try:
        start, end = time.fromisoformat(business_start), time.fromisoformat(business_end)
        end_time = time.fromisoformat(shift_end_time) if shift_end_time else None
        voice_max = Decimal(voice_max_amount.replace(",", "")) if voice_max_amount.strip() else None
    except (ValueError, InvalidOperation):
        return await settings_page(ctx, error="Утгуудыг зөв форматаар оруулна уу.")
    if (
        start >= end
        or not set(business_days) <= set(range(7))
        or (voice_max is not None and voice_max <= 0)
    ):
        return await settings_page(ctx, error="Ажлын цаг болон дүнгийн утгыг шалгана уу.")
    async with ctx.session() as s:
        from khugjil.db.models import Tenant

        t = await s.get(Tenant, ctx.tenant_id)
        if t is None:  # pragma: no cover
            raise HTTPException(404)
        t.accent, t.toast_seconds, t.health_silence_hours = (
            accent,
            toast_seconds,
            health_silence_hours,
        )
        t.shift_idle_hours, t.shift_end_time = shift_idle_hours, end_time
        t.business_hours = {
            "days": sorted(set(business_days)),
            "start": business_start,
            "end": business_end,
        }
        t.voice_enabled, t.voice_max_amount = voice_enabled, voice_max
        t.show_today_totals, t.include_own_transfers = show_today_totals, include_own_transfers
        await ctx.audit(s, "settings.update", object_type="tenant", object_id=ctx.tenant_id)
        device_ids = list(await s.scalars(select(Device.id).where(Device.status == "active")))
    for device_id in device_ids:
        await ctx.rt.bus.publish(device_id, "settings_changed")
    return redirect("/settings?saved=1")


# ---------------------------------------------------------------- audit log


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(ctx: Manager, page: int = 1) -> HTMLResponse:
    page = max(1, min(page, 10_000))
    async with ctx.session() as s:
        total = await s.scalar(select(func.count()).select_from(AuditLog)) or 0
        rows = list(
            (
                await s.execute(
                    select(AuditLog)
                    .order_by(AuditLog.at.desc())
                    .limit(PAGE_SIZE)
                    .offset((page - 1) * PAGE_SIZE)
                )
            ).scalars()
        )
        names = {m.id: m.display_name for m in (await s.execute(select(Member))).scalars()}
    return ctx.render(
        "audit.html", rows=rows, names=names, page=page, pages=max(1, -(-int(total) // PAGE_SIZE))
    )
