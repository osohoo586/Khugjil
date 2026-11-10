"""Cashier JSON API v1 (app.<DOMAIN>/api/v1). Versioned and documented so a Phase 2 desktop
agent can use it unchanged. Every endpoint is scoped to the authenticated device."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from khugjil.auth.devices import (
    DeviceAuth,
    PairingError,
    ShiftError,
    authenticate_device,
    open_shift,
    record_pin_failure,
    redeem_pairing,
    shift_candidates,
    start_shift,
)
from khugjil.auth.secrets import WeakSecretError, check_pin, hash_secret
from khugjil.crypto import field_aad
from khugjil.db.models import (
    BankAccount,
    Device,
    DeviceGroup,
    GroupAccount,
    GroupMember,
    Member,
    PushSubscription,
    RecipientGroup,
    Shift,
    Tenant,
    Transaction,
)
from khugjil.db.tenancy import tenant_session
from khugjil.delivery.service import ack, notification_for
from khugjil.logs import get_logger
from khugjil.ratelimit import RateLimiter
from khugjil.runtime import Runtime
from khugjil.services.consent import give_consent
from khugjil.services.invites import InviteError, consume_invite, inspect_invite
from khugjil.services.shifts import end_open_shifts
from khugjil.settings import BANK_TZ
from khugjil.web.base import RuntimeHolder

DEVICE_COOKIE = "__Host-khj_dev"
COOKIE_MAX_AGE = 400 * 24 * 3600
VERIFY_WINDOW = timedelta(minutes=15)
LIST_LIMIT = 10

MESSAGES = {
    "unauthorized": "Төхөөрөмж холбогдоогүй байна. Админаас холбох код авна уу.",
    "invalid_code": "Код буруу эсвэл хугацаа нь дууссан байна. Админаас шинэ код авна уу.",
    "rate_limited": "Хэт олон оролдлого хийлээ. Түр хүлээгээд дахин оролдоно уу.",
    "not_allowed": "Энэ хэрэглэгч энэ төхөөрөмж дээр ээлж эхлүүлэх эрхгүй байна.",
    "locked": (
        "Олон удаа буруу оруулсан тул түр хаагдсан. Дараа дахин оролдох эсвэл админд хандана уу."
    ),
    "wrong_pin": "ПИН код буруу байна. Дахин оролдоно уу.",
    "consent_required": "Таны зөвшөөрөл идэвхгүй байна. Админаас шинэ урилга авна уу.",
    "no_shift": "Ээлж эхлээгүй байна. Эхлээд ээлжээ эхлүүлнэ үү.",
    "bad_request": "Хүсэлт буруу байна.",
    "invite_invalid": (
        "Урилгын холбоос хүчингүй эсвэл хугацаа нь дууссан байна. Админаас шинэ урилга авна уу."
    ),
    "weak_pin": "ПИН код хэт энгийн байна. Өөр 6 оронтой код сонгоно уу.",
    "not_found": "Олдсонгүй.",
}


def fail(status: int, code: str) -> HTTPException:
    return HTTPException(status, detail={"error": code, "message": MESSAGES[code]})


def utcnow() -> datetime:
    return datetime.now(UTC)


def get_holder(request: Request) -> RuntimeHolder:
    holder: RuntimeHolder = request.app.state.holder
    return holder


def get_rt(holder: Annotated[RuntimeHolder, Depends(get_holder)]) -> Runtime:
    return holder.runtime()


Rt = Annotated[Runtime, Depends(get_rt)]


def set_device_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        DEVICE_COOKIE,
        token,
        max_age=COOKIE_MAX_AGE,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


async def device_auth(request: Request, response: Response, rt: Rt) -> DeviceAuth:
    token = request.cookies.get(DEVICE_COOKIE)
    auth = await authenticate_device(rt.sm, token, utcnow()) if token else None
    if auth is None:
        raise fail(401, "unauthorized")
    if auth.rotated_token:
        set_device_cookie(response, auth.rotated_token)
    return auth


Device_ = Annotated[DeviceAuth, Depends(device_auth)]
router = APIRouter(prefix="/api/v1")


async def visible_account_ids(
    s: AsyncSession, device_id: uuid.UUID, member_id: uuid.UUID
) -> list[uuid.UUID]:
    """Active accounts in active groups that contain both this device and this member."""
    dg = select(DeviceGroup.group_id).where(DeviceGroup.device_id == device_id)
    mg = select(GroupMember.group_id).where(GroupMember.member_id == member_id)
    rows = await s.scalars(
        select(BankAccount.id)
        .join(GroupAccount, GroupAccount.account_id == BankAccount.id)
        .join(RecipientGroup, RecipientGroup.id == GroupAccount.group_id)
        .where(
            GroupAccount.group_id.in_(dg),
            GroupAccount.group_id.in_(mg),
            RecipientGroup.status == "active",
            BankAccount.status == "active",
        )
        .distinct()
    )
    return list(rows)


async def _require_shift(s: AsyncSession, device_id: uuid.UUID) -> Shift:
    shift = await open_shift(s, device_id)
    if shift is None:
        raise fail(409, "no_shift")
    return shift


# ---------------------------------------------------------------- pairing


class PairBody(BaseModel):
    token: str | None = Field(default=None, max_length=128)
    code: str | None = Field(default=None, max_length=32)


@router.post("/pair")
async def pair(body: PairBody, request: Request, response: Response, rt: Rt) -> dict[str, Any]:
    limiter = RateLimiter(rt.redis)
    ip = request.client.host if request.client else "unknown"
    short_only = bool(body.code) and not body.token
    if short_only and not (
        await limiter.allow(f"pair-ip:{ip}", 5, 600)
        and await limiter.allow("pair-global", 100, 600)
    ):
        raise fail(429, "rate_limited")
    try:
        paired = await redeem_pairing(
            rt.sm, rt.crypto, token=body.token, short_code=body.code, now=utcnow()
        )
    except PairingError:
        get_logger().warning("cashier.pair_failed", peer_ip=ip)
        raise fail(400, "invalid_code") from None
    set_device_cookie(response, paired.token)
    return {"paired": True}


# ---------------------------------------------------------------- device info + shifts


@router.get("/device")
async def device_info(auth: Device_, rt: Rt) -> dict[str, Any]:
    async with tenant_session(rt.sm, auth.tenant_id) as s:
        tenant = await s.get(Tenant, auth.tenant_id)
        device = await s.get(Device, auth.device_id)
        if tenant is None or device is None:  # pragma: no cover - auth guarantees both
            raise fail(401, "unauthorized")
        shift = await open_shift(s, auth.device_id)
        shift_member = await s.get(Member, shift.member_id) if shift else None
        candidates = await shift_candidates(s, auth.device_id)
        accounts: list[str] = []
        if shift:
            ids = await visible_account_ids(s, auth.device_id, shift.member_id)
            accounts = list(
                await s.scalars(select(BankAccount.alias).where(BankAccount.id.in_(ids)))
            )
        account_count = await s.scalar(select(func.count()).select_from(BankAccount)) or 0
    return {
        "device_name": device.name,
        "tenant_name": tenant.name,
        "accent": tenant.accent,
        "toast_seconds": tenant.toast_seconds,
        "voice_enabled": tenant.voice_enabled,
        "voice_max_amount": str(tenant.voice_max_amount) if tenant.voice_max_amount else None,
        "show_today_totals": tenant.show_today_totals,
        "multiple_accounts": account_count > 1,
        "accounts": accounts,
        "members": [{"id": str(m.id), "display_name": m.display_name} for m in candidates],
        "shift": (
            {
                "id": str(shift.id),
                "member_id": str(shift.member_id),
                "display_name": shift_member.display_name if shift_member else "",
                "started_at": shift.started_at.isoformat(),
            }
            if shift
            else None
        ),
        "vapid_public_key": rt.settings.vapid_public_key,
    }


class ShiftStartBody(BaseModel):
    member_id: uuid.UUID
    pin: str = Field(min_length=1, max_length=12)


@router.post("/shift/start")
async def shift_start(body: ShiftStartBody, auth: Device_, rt: Rt) -> dict[str, Any]:
    limiter = RateLimiter(rt.redis)
    if not await limiter.allow(f"pin:{auth.device_id}", 10, 600):
        raise fail(429, "rate_limited")
    try:
        async with tenant_session(rt.sm, auth.tenant_id) as s:
            shift = await start_shift(
                s, auth.tenant_id, auth.device_id, body.member_id, body.pin, utcnow()
            )
    except ShiftError as exc:
        # Failure counters must persist even though the transaction raised: record them separately.
        if exc.code == "wrong_pin":
            await record_pin_failure(rt.sm, auth.tenant_id, body.member_id, utcnow())
        raise fail(403 if exc.code != "locked" else 423, exc.code) from None
    await rt.bus.publish(auth.device_id, "settings_changed")
    return {"shift_id": str(shift.id), "started_at": shift.started_at.isoformat()}


@router.post("/shift/end")
async def shift_end(auth: Device_, rt: Rt) -> dict[str, Any]:
    async with tenant_session(rt.sm, auth.tenant_id) as s:
        ended = await end_open_shifts(s, reason="manual", now=utcnow(), device_id=auth.device_id)
    return {"ended": len(ended)}


@router.post("/shift/activity")
async def shift_activity(auth: Device_, rt: Rt) -> dict[str, Any]:
    async with tenant_session(rt.sm, auth.tenant_id) as s:
        shift = await _require_shift(s, auth.device_id)
        shift.last_activity_at = utcnow()
    return {"ok": True}


# ---------------------------------------------------------------- transactions


@router.get("/transactions")
async def transactions(auth: Device_, rt: Rt, since_seq: int | None = None) -> dict[str, Any]:
    async with tenant_session(rt.sm, auth.tenant_id) as s:
        shift = await _require_shift(s, auth.device_id)
        ids = await visible_account_ids(s, auth.device_id, shift.member_id)
        stmt = select(Transaction).where(
            Transaction.bank_account_id.in_(ids), Transaction.received_at >= shift.started_at
        )
        if since_seq is not None:
            stmt = stmt.where(Transaction.seq > since_seq)
        rows = list(
            (await s.execute(stmt.order_by(Transaction.seq.desc()).limit(LIST_LIMIT))).scalars()
        )
        notes = [
            (await notification_for(rt.delivery_deps(), s, auth.tenant_id, t)).to_api()
            for t in rows
        ]
    return {"items": notes}


@router.post("/transactions/{txn_id}/ack")
async def ack_transaction(txn_id: uuid.UUID, auth: Device_, rt: Rt) -> dict[str, Any]:
    return {"acked": await ack(rt.delivery_deps(), auth.tenant_id, auth.device_id, txn_id)}


@router.get("/verify")
async def verify_amount(amount: str, auth: Device_, rt: Rt) -> dict[str, Any]:
    if not await RateLimiter(rt.redis).allow(f"verify:{auth.device_id}", 60, 60):
        raise fail(429, "rate_limited")
    try:
        value = Decimal(amount.replace(",", "").replace(" ", ""))
    except InvalidOperation:
        raise fail(400, "bad_request") from None
    if not value.is_finite() or value <= 0 or value > Decimal("1e15"):
        raise fail(400, "bad_request")
    now = utcnow()
    async with tenant_session(rt.sm, auth.tenant_id) as s:
        shift = await _require_shift(s, auth.device_id)
        ids = await visible_account_ids(s, auth.device_id, shift.member_id)
        rows = (
            await s.execute(
                select(Transaction)
                .where(
                    Transaction.bank_account_id.in_(ids),
                    Transaction.amount == value,
                    Transaction.received_at >= now - VERIFY_WINDOW,
                )
                .order_by(Transaction.seq.desc())
                .limit(5)
            )
        ).scalars()
        matches = [
            (await notification_for(rt.delivery_deps(), s, auth.tenant_id, t)).to_api()
            for t in rows
        ]
    return {"matches": matches}


@router.get("/summary/today")
async def summary_today(auth: Device_, rt: Rt) -> dict[str, Any]:
    tz = ZoneInfo(BANK_TZ)
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    async with tenant_session(rt.sm, auth.tenant_id) as s:
        tenant = await s.get(Tenant, auth.tenant_id)
        if tenant is None or not tenant.show_today_totals:
            raise fail(404, "not_found")
        shift = await _require_shift(s, auth.device_id)
        ids = await visible_account_ids(s, auth.device_id, shift.member_id)
        count, total = (
            await s.execute(
                select(func.count(), func.coalesce(func.sum(Transaction.amount), 0)).where(
                    Transaction.bank_account_id.in_(ids),
                    Transaction.currency == "MNT",
                    Transaction.txn_at >= start,
                )
            )
        ).one()
    from khugjil.delivery.format import format_amount

    return {"count": int(count), "total_text": format_amount(Decimal(total), "MNT")}


# ---------------------------------------------------------------- web push


class PushKeys(BaseModel):
    p256dh: str = Field(min_length=10, max_length=200)
    auth: str = Field(min_length=10, max_length=100)


class PushBody(BaseModel):
    endpoint: str = Field(min_length=12, max_length=1000, pattern=r"^https://")
    keys: PushKeys


@router.post("/push/subscribe")
async def push_subscribe(body: PushBody, auth: Device_, rt: Rt) -> dict[str, Any]:
    async with tenant_session(rt.sm, auth.tenant_id) as s:
        shift = await _require_shift(s, auth.device_id)
        await s.execute(delete(PushSubscription).where(PushSubscription.shift_id == shift.id))
        s.add(
            PushSubscription(
                tenant_id=auth.tenant_id,
                device_id=auth.device_id,
                member_id=shift.member_id,
                shift_id=shift.id,
                endpoint_enc=rt.crypto.encrypt(
                    body.endpoint, field_aad(auth.tenant_id, "push_subscription", "endpoint_enc")
                ),
                p256dh=body.keys.p256dh,
                auth_secret_enc=rt.crypto.encrypt(
                    body.keys.auth,
                    field_aad(auth.tenant_id, "push_subscription", "auth_secret_enc"),
                ),
            )
        )
    return {"subscribed": True}


@router.delete("/push/subscribe")
async def push_unsubscribe(auth: Device_, rt: Rt) -> dict[str, Any]:
    async with tenant_session(rt.sm, auth.tenant_id) as s:
        await s.execute(
            delete(PushSubscription).where(PushSubscription.device_id == auth.device_id)
        )
    return {"subscribed": False}


# ---------------------------------------------------------------- cashier invites


class InviteToken(BaseModel):
    token: str = Field(min_length=20, max_length=128)


class InviteAccept(InviteToken):
    pin: str = Field(min_length=6, max_length=6)
    consent_version: int
    accept: bool


@router.post("/invite/inspect")
async def invite_inspect(body: InviteToken, request: Request, rt: Rt) -> dict[str, Any]:
    ip = request.client.host if request.client else "unknown"
    if not await RateLimiter(rt.redis).allow(f"invite:{ip}", 20, 600):
        raise fail(429, "rate_limited")
    try:
        view = await inspect_invite(rt.sm, body.token, utcnow(), rt.settings.retention_days)
    except InviteError:
        raise fail(400, "invite_invalid") from None
    if view.role != "cashier":
        raise fail(400, "invite_invalid")
    return {
        "tenant_name": view.tenant_name,
        "display_name": view.display_name,
        "consent_version": view.consent_version,
        "consent_text": view.consent_text,
    }


@router.post("/invite/accept")
async def invite_accept(body: InviteAccept, rt: Rt) -> dict[str, Any]:
    now = utcnow()
    try:
        view = await inspect_invite(rt.sm, body.token, now, rt.settings.retention_days)
        check_pin(body.pin)
    except InviteError:
        raise fail(400, "invite_invalid") from None
    except WeakSecretError:
        raise fail(400, "weak_pin") from None
    if view.role != "cashier" or not body.accept or body.consent_version != view.consent_version:
        raise fail(400, "invite_invalid")
    try:
        async with tenant_session(rt.sm, view.tenant_id) as s:
            await consume_invite(s, view.invite_id, now)
            member = await s.get(Member, view.member_id, with_for_update=True)
            if member is None:  # pragma: no cover
                raise InviteError("invalid")
            member.pin_hash = hash_secret(body.pin)
            member.status = "active"
            member.pin_failures, member.pin_locked_until = 0, None
            await give_consent(s, view.tenant_id, member.id, "web", body.consent_version)
    except InviteError:
        raise fail(400, "invite_invalid") from None
    return {"accepted": True}
