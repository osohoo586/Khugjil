"""Admin pages for members (Хэрэглэгчид) and devices (Төхөөрөмжүүд)."""

from __future__ import annotations

import uuid
from datetime import time, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import delete, func, select

from khugjil.admin.core import Manager, Owner, redirect, utcnow
from khugjil.admin.views_pages import online
from khugjil.auth.devices import PairingError, create_pairing, revoke_device
from khugjil.auth.sessions import end_member_sessions
from khugjil.crypto import new_token, token_hash
from khugjil.db.models import (
    Consent,
    Device,
    DeviceGroup,
    DutyWindow,
    GroupMember,
    Member,
    RecipientGroup,
    Shift,
    TelegramLink,
    TelegramLinkCode,
)
from khugjil.services.consent import revoke_consent
from khugjil.services.invites import create_invite, invite_link
from khugjil.services.shifts import end_open_shifts

router = APIRouter()
TELEGRAM_CODE_TTL = timedelta(minutes=10)


def manageable_roles(actor_role: str) -> list[str]:
    """Owners manage everyone; admins manage cashiers only (PLAN §1.8, Q20)."""
    return ["owner", "admin", "viewer", "cashier"] if actor_role == "owner" else ["cashier"]


async def _member_or_404(s: Any, member_id: uuid.UUID, actor_role: str) -> Member:
    member: Member | None = await s.get(Member, member_id)
    if member is None or member.role not in manageable_roles(actor_role):
        raise HTTPException(404)
    return member


# ---------------------------------------------------------------- members


@router.get("/members", response_class=HTMLResponse)
async def members(ctx: Manager) -> HTMLResponse:
    async with ctx.session() as s:
        rows = list(
            (
                await s.execute(
                    select(Member)
                    .where(Member.status != "revoked")
                    .order_by(Member.role, Member.display_name)
                )
            ).scalars()
        )
        groups = {g.id: g.name for g in (await s.execute(select(RecipientGroup))).scalars()}
        member_groups: dict[uuid.UUID, list[str]] = {}
        for gm in (await s.execute(select(GroupMember))).scalars():
            member_groups.setdefault(gm.member_id, []).append(groups.get(gm.group_id, ""))
        consents: dict[uuid.UUID, list[Consent]] = {}
        for c in (await s.execute(select(Consent).where(Consent.revoked_at.is_(None)))).scalars():
            consents.setdefault(c.member_id, []).append(c)
        windows: dict[uuid.UUID, list[DutyWindow]] = {}
        for w in (await s.execute(select(DutyWindow).order_by(DutyWindow.weekday))).scalars():
            windows.setdefault(w.member_id, []).append(w)
        linked = set(
            await s.scalars(select(TelegramLink.member_id).where(TelegramLink.status == "active"))
        )
    return ctx.render(
        "members.html",
        members=rows,
        member_groups=member_groups,
        consents=consents,
        windows=windows,
        linked=linked,
        roles=manageable_roles(ctx.role),
        groups=groups,
    )


@router.post("/members")
async def member_invite(
    ctx: Manager,
    display_name: Annotated[str, Form(min_length=1, max_length=60)],
    role: Annotated[str, Form(pattern="^(owner|admin|viewer|cashier)$")],
    group_ids: Annotated[list[uuid.UUID], Form()] = [],  # noqa: B006
) -> Response:
    if role not in manageable_roles(ctx.role):
        raise HTTPException(403)
    async with ctx.session() as s:
        valid = set(
            await s.scalars(select(RecipientGroup.id).where(RecipientGroup.id.in_(group_ids)))
        )
        if valid != set(group_ids):
            raise HTTPException(404)
        member = Member(tenant_id=ctx.tenant_id, display_name=display_name.strip(), role=role)
        s.add(member)
        await s.flush()
        s.add_all(
            [GroupMember(tenant_id=ctx.tenant_id, group_id=g, member_id=member.id) for g in valid]
        )
        token = await create_invite(
            s, ctx.tenant_id, member.id, created_by=ctx.member.id, now=utcnow()
        )
        await ctx.audit(
            s, "member.invite", object_type="member", object_id=member.id, label=member.display_name
        )
    link = invite_link(ctx.rt.settings.app_host, ctx.rt.settings.admin_host, role, token)
    return ctx.render("invite_created.html", member=member, link=link)


@router.get("/members/{member_id}", response_class=HTMLResponse)
async def member_edit(ctx: Manager, member_id: uuid.UUID) -> HTMLResponse:
    async with ctx.session() as s:
        member = await _member_or_404(s, member_id, ctx.role)
        groups = list(
            (await s.execute(select(RecipientGroup).order_by(RecipientGroup.name))).scalars()
        )
        selected = set(
            await s.scalars(select(GroupMember.group_id).where(GroupMember.member_id == member_id))
        )
        windows = list(
            (
                await s.execute(
                    select(DutyWindow)
                    .where(DutyWindow.member_id == member_id)
                    .order_by(DutyWindow.weekday, DutyWindow.start_local)
                )
            ).scalars()
        )
    return ctx.render(
        "member_edit.html",
        member=member,
        groups=groups,
        selected=selected,
        windows=windows,
        roles=manageable_roles(ctx.role),
    )


@router.post("/members/{member_id}")
async def member_save(
    ctx: Manager,
    member_id: uuid.UUID,
    display_name: Annotated[str, Form(min_length=1, max_length=60)],
    role: Annotated[str, Form(pattern="^(owner|admin|viewer|cashier)$")],
    group_ids: Annotated[list[uuid.UUID], Form()] = [],  # noqa: B006
) -> Response:
    if role not in manageable_roles(ctx.role):
        raise HTTPException(403)
    async with ctx.session() as s:
        member = await _member_or_404(s, member_id, ctx.role)
        if member.role == "owner" and role != "owner" and await _last_owner(s, member_id):
            raise HTTPException(409)
        valid = set(
            await s.scalars(select(RecipientGroup.id).where(RecipientGroup.id.in_(group_ids)))
        )
        if valid != set(group_ids):
            raise HTTPException(404)
        member.display_name, member.role = display_name.strip(), role
        await s.execute(delete(GroupMember).where(GroupMember.member_id == member_id))
        s.add_all(
            [GroupMember(tenant_id=ctx.tenant_id, group_id=g, member_id=member_id) for g in valid]
        )
        await ctx.audit(
            s, "member.update", object_type="member", object_id=member_id, label=member.display_name
        )
    return redirect("/members")


async def _last_owner(s: Any, member_id: uuid.UUID) -> bool:
    others = await s.scalar(
        select(func.count())
        .select_from(Member)
        .where(Member.role == "owner", Member.status == "active", Member.id != member_id)
    )
    return not others


@router.post("/members/{member_id}/schedule")
async def member_schedule_add(
    ctx: Manager,
    member_id: uuid.UUID,
    weekday: Annotated[int, Form(ge=0, le=6)],
    start: Annotated[str, Form(pattern=r"^\d{2}:\d{2}$")],
    end: Annotated[str, Form(pattern=r"^\d{2}:\d{2}$")],
) -> Response:
    try:
        start_t, end_t = time.fromisoformat(start), time.fromisoformat(end)
    except ValueError:
        raise HTTPException(400) from None
    if start_t >= end_t:
        raise HTTPException(400)
    async with ctx.session() as s:
        member = await _member_or_404(s, member_id, ctx.role)
        s.add(
            DutyWindow(
                tenant_id=ctx.tenant_id,
                member_id=member_id,
                weekday=weekday,
                start_local=start_t,
                end_local=end_t,
            )
        )
        await ctx.audit(
            s,
            "member.schedule_add",
            object_type="member",
            object_id=member_id,
            label=member.display_name,
        )
    return redirect(f"/members/{member_id}")


@router.post("/members/{member_id}/schedule/{window_id}/delete")
async def member_schedule_delete(
    ctx: Manager, member_id: uuid.UUID, window_id: uuid.UUID
) -> Response:
    async with ctx.session() as s:
        member = await _member_or_404(s, member_id, ctx.role)
        await s.execute(
            delete(DutyWindow).where(DutyWindow.id == window_id, DutyWindow.member_id == member_id)
        )
        await ctx.audit(
            s,
            "member.schedule_remove",
            object_type="member",
            object_id=member_id,
            label=member.display_name,
        )
    return redirect(f"/members/{member_id}")


@router.post("/members/{member_id}/reinvite")
async def member_reinvite(ctx: Manager, member_id: uuid.UUID) -> Response:
    """PIN / password reset: nobody learns the old secret; the member sets a new one."""
    async with ctx.session() as s:
        member = await _member_or_404(s, member_id, ctx.role)
        if member.id == ctx.member.id:
            raise HTTPException(409)
        member.pin_hash = None
        member.password_hash = None
        member.totp_secret_enc = None
        member.status = "invited"
        await end_open_shifts(s, reason="member_revoked", now=utcnow(), member_id=member_id)
        token = await create_invite(
            s, ctx.tenant_id, member_id, created_by=ctx.member.id, now=utcnow()
        )
        await ctx.audit(
            s,
            "member.reinvite",
            object_type="member",
            object_id=member_id,
            label=member.display_name,
        )
    await end_member_sessions(ctx.rt.sm, ctx.tenant_id, member_id)
    link = invite_link(ctx.rt.settings.app_host, ctx.rt.settings.admin_host, member.role, token)
    return ctx.render("invite_created.html", member=member, link=link)


@router.post("/members/{member_id}/revoke")
async def member_revoke(ctx: Manager, member_id: uuid.UUID) -> Response:
    now = utcnow()
    async with ctx.session() as s:
        member = await _member_or_404(s, member_id, ctx.role)
        if member.id == ctx.member.id or (
            member.role == "owner" and await _last_owner(s, member_id)
        ):
            raise HTTPException(409)
        member.status, member.revoked_at = "revoked", now
        await ctx.audit(
            s, "member.revoke", object_type="member", object_id=member_id, label=member.display_name
        )
    await revoke_consent(ctx.rt.sm, ctx.rt.bus, ctx.tenant_id, member_id, via="admin", now=now)
    async with ctx.session() as s:
        ended = await end_open_shifts(s, reason="member_revoked", now=now, member_id=member_id)
    for shift_id, device_id in ended:
        await ctx.rt.bus.publish(device_id, "shift_ended", shift_id=shift_id)
    await end_member_sessions(ctx.rt.sm, ctx.tenant_id, member_id)
    return redirect("/members")


@router.post("/members/{member_id}/consent/revoke")
async def member_consent_revoke(ctx: Manager, member_id: uuid.UUID) -> Response:
    async with ctx.session() as s:
        member = await _member_or_404(s, member_id, ctx.role)
        await ctx.audit(
            s,
            "consent.revoke",
            object_type="member",
            object_id=member_id,
            label=member.display_name,
        )
    await revoke_consent(ctx.rt.sm, ctx.rt.bus, ctx.tenant_id, member_id, via="admin", now=utcnow())
    return redirect("/members")


@router.post("/me/telegram")
async def telegram_link_code(ctx: Owner) -> Response:
    username = ctx.rt.settings.telegram_bot_username
    if not username:
        raise HTTPException(404)
    code = new_token(12)
    async with ctx.session() as s:
        s.add(
            TelegramLinkCode(
                tenant_id=ctx.tenant_id,
                member_id=ctx.member.id,
                code_hash=token_hash(code),
                expires_at=utcnow() + TELEGRAM_CODE_TTL,
            )
        )
        await ctx.audit(s, "telegram.link_code", object_type="member", object_id=ctx.member.id)
    return ctx.render("telegram_link.html", link=f"https://t.me/{username}?start={code}")


# ---------------------------------------------------------------- devices


@router.get("/devices", response_class=HTMLResponse)
async def devices(ctx: Manager) -> HTMLResponse:
    now = utcnow()
    async with ctx.session() as s:
        rows = list(
            (
                await s.execute(
                    select(Device).where(Device.status == "active").order_by(Device.name)
                )
            ).scalars()
        )
        groups = {
            g.id: g
            for g in (
                await s.execute(select(RecipientGroup).order_by(RecipientGroup.name))
            ).scalars()
        }
        device_groups: dict[uuid.UUID, list[str]] = {}
        for dg in (await s.execute(select(DeviceGroup))).scalars():
            device_groups.setdefault(dg.device_id, []).append(groups[dg.group_id].name)
        on_shift = {
            sh.device_id: sh.member_id
            for sh in (await s.execute(select(Shift).where(Shift.ended_at.is_(None)))).scalars()
        }
        names = {m.id: m.display_name for m in (await s.execute(select(Member))).scalars()}
    return ctx.render(
        "devices.html",
        devices=rows,
        device_groups=device_groups,
        groups=list(groups.values()),
        on_shift={k: names.get(v, "") for k, v in on_shift.items()},
        online={d.id: online(d, now) for d in rows},
    )


@router.post("/devices/pair", response_class=HTMLResponse)
async def device_pair(
    ctx: Manager,
    device_name: Annotated[str, Form(min_length=1, max_length=60)],
    group_ids: Annotated[list[uuid.UUID], Form()] = [],  # noqa: B006
) -> HTMLResponse:
    try:
        async with ctx.session() as s:
            pairing = await create_pairing(
                s,
                ctx.rt.crypto,
                ctx.tenant_id,
                device_name=device_name,
                group_ids=group_ids,
                created_by=ctx.member.id,
                now=utcnow(),
            )
            await ctx.audit(
                s,
                "device.pairing_code",
                object_type="pairing_code",
                object_id=pairing.pairing_id,
                label=device_name,
            )
    except PairingError:
        raise HTTPException(400) from None
    url = f"https://{ctx.rt.settings.app_host}/pair#t={pairing.token}"
    template = "_pair_dialog.html" if ctx.request.headers.get("hx-request") else "pair.html"
    return ctx.render(
        template,
        pairing=pairing,
        url=url,
        device_name=device_name,
        app_host=ctx.rt.settings.app_host,
    )


@router.get("/devices/{device_id}", response_class=HTMLResponse)
async def device_edit(ctx: Manager, device_id: uuid.UUID) -> HTMLResponse:
    async with ctx.session() as s:
        device = await s.get(Device, device_id)
        if device is None or device.status != "active":
            raise HTTPException(404)
        groups = list(
            (await s.execute(select(RecipientGroup).order_by(RecipientGroup.name))).scalars()
        )
        selected = set(
            await s.scalars(select(DeviceGroup.group_id).where(DeviceGroup.device_id == device_id))
        )
    return ctx.render("device_edit.html", device=device, groups=groups, selected=selected)


@router.post("/devices/{device_id}")
async def device_save(
    ctx: Manager,
    device_id: uuid.UUID,
    name: Annotated[str, Form(min_length=1, max_length=60)],
    group_ids: Annotated[list[uuid.UUID], Form()] = [],  # noqa: B006
) -> Response:
    async with ctx.session() as s:
        device = await s.get(Device, device_id)
        if device is None or device.status != "active":
            raise HTTPException(404)
        valid = set(
            await s.scalars(select(RecipientGroup.id).where(RecipientGroup.id.in_(group_ids)))
        )
        if valid != set(group_ids):
            raise HTTPException(404)
        device.name = name.strip()
        await s.execute(delete(DeviceGroup).where(DeviceGroup.device_id == device_id))
        s.add_all(
            [DeviceGroup(tenant_id=ctx.tenant_id, device_id=device_id, group_id=g) for g in valid]
        )
        await ctx.audit(
            s, "device.update", object_type="device", object_id=device_id, label=device.name
        )
    await ctx.rt.bus.publish(device_id, "settings_changed")
    return redirect("/devices")


@router.post("/devices/{device_id}/revoke")
async def device_revoke(ctx: Manager, device_id: uuid.UUID) -> Response:
    async with ctx.session() as s:
        device = await s.get(Device, device_id)
        if device is None:
            raise HTTPException(404)
        await revoke_device(s, device_id, by=ctx.member.id, now=utcnow())
        await ctx.audit(
            s, "device.revoke", object_type="device", object_id=device_id, label=device.name
        )
    await ctx.rt.bus.publish(device_id, "device_revoked")
    return redirect("/devices")
