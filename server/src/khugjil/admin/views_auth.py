"""Sign-in (password, then TOTP), step-up, sign-out, and invitations for roles that sign in."""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select

from khugjil.admin.core import (
    ADMIN_COOKIE,
    INVITE_COOKIE,
    PRE_AUTH_COOKIE,
    CtxDep,
    env,
    get_rt,
    redirect,
    safe_next,
    set_cookie,
    utcnow,
)
from khugjil.auth.secrets import (
    WeakSecretError,
    check_password,
    hash_secret,
    lockout_until,
    new_recovery_codes,
    new_totp_secret,
    totp_uri,
    verify_secret,
    verify_totp,
)
from khugjil.auth.sessions import create_session, end_session, mark_stepped_up
from khugjil.crypto import field_aad, new_token, token_hash
from khugjil.db.models import Member, RecoveryCode, TenantTermsAcceptance
from khugjil.db.tenancy import tenant_session
from khugjil.ratelimit import RateLimiter
from khugjil.runtime import Runtime
from khugjil.services.consent import current_document, give_consent, render_document
from khugjil.services.invites import LOGIN_ROLES, InviteError, consume_invite, inspect_invite

router = APIRouter()
Rt = Annotated[Runtime, Depends(get_rt)]
PRE_AUTH_TTL = 300
INVITE_TTL = 1800


def page(template: str, status: int = 200, **values: Any) -> HTMLResponse:
    return HTMLResponse(env.get_template(template).render(ctx=None, **values), status_code=status)


async def _redis_put(rt: Runtime, prefix: str, payload: dict[str, Any], ttl: int) -> str:
    key = new_token(24)
    blob = rt.crypto.encrypt(json.dumps(payload), field_aad(None, "redis", prefix))
    await rt.redis.set(f"khj:{prefix}:{token_hash(key).hex()}", blob, ex=ttl)
    return key


async def _redis_get(rt: Runtime, prefix: str, key: str | None) -> dict[str, Any] | None:
    if not key:
        return None
    blob = await rt.redis.get(f"khj:{prefix}:{token_hash(key).hex()}")
    if blob is None:
        return None
    data: dict[str, Any] = json.loads(rt.crypto.decrypt(blob, field_aad(None, "redis", prefix)))
    return data


async def _redis_drop(rt: Runtime, prefix: str, key: str | None) -> None:
    if key:
        await rt.redis.delete(f"khj:{prefix}:{token_hash(key).hex()}")


# ---------------------------------------------------------------- sign in

LOGIN_ERROR = "Нэвтрэх нэр эсвэл нууц үг буруу байна."


@router.get("/login", response_class=HTMLResponse)
async def login_form() -> HTMLResponse:
    return page("login.html", error=None)


@router.post("/login")
async def login_submit(
    request: Request,
    rt: Rt,
    login_name: Annotated[str, Form(max_length=120)],
    password: Annotated[str, Form(max_length=200)],
) -> Response:
    ip = request.client.host if request.client else "unknown"
    limiter = RateLimiter(rt.redis)
    if not (
        await limiter.allow(f"login-ip:{ip}", 20, 600)
        and await limiter.allow(f"login-name:{login_name.lower()}", 10, 600)
    ):
        return page(
            "login.html",
            429,
            error="Хэт олон оролдлого хийлээ. 10 минутын дараа дахин оролдоно уу.",
        )
    now = utcnow()
    found = await rt.resolver.login(login_name.strip())
    if found is None:
        verify_secret(None, password)
        return page("login.html", 401, error=LOGIN_ERROR)
    tenant_id, member_id = found
    async with tenant_session(rt.sm, tenant_id) as s:
        member = await s.get(Member, member_id, with_for_update=True)
        if member is None or member.role not in LOGIN_ROLES:
            return page("login.html", 401, error=LOGIN_ERROR)
        if member.login_locked_until and member.login_locked_until > now:
            return page("login.html", 423, error="Түр хаагдсан байна. Дараа дахин оролдоно уу.")
        if not verify_secret(member.password_hash, password):
            member.login_failures += 1
            member.login_locked_until = lockout_until(member.login_failures, now)
            return page("login.html", 401, error=LOGIN_ERROR)
        member.login_failures, member.login_locked_until = 0, None
    key = await _redis_put(rt, "preauth", {"t": str(tenant_id), "m": str(member_id)}, PRE_AUTH_TTL)
    response = redirect("/login/totp")
    set_cookie(response, PRE_AUTH_COOKIE, key, PRE_AUTH_TTL)
    return response


@router.get("/login/totp", response_class=HTMLResponse)
async def totp_form() -> HTMLResponse:
    return page("totp.html", error=None)


async def _check_second_factor(
    rt: Runtime, tenant_id: uuid.UUID, member_id: uuid.UUID, code: str
) -> bool:
    now = utcnow()
    async with tenant_session(rt.sm, tenant_id) as s:
        member = await s.get(Member, member_id, with_for_update=True)
        if member is None or member.totp_secret_enc is None:
            return False
        secret = rt.crypto.decrypt_str(
            member.totp_secret_enc, field_aad(tenant_id, "member", "totp_secret_enc")
        )
        step = verify_totp(secret, code, now, member.totp_last_step)
        if step is not None:
            member.totp_last_step = step
            return True
        normalized = code.strip().upper()
        if len(normalized) == 11:
            row = (
                await s.execute(
                    select(RecoveryCode).where(
                        RecoveryCode.member_id == member_id,
                        RecoveryCode.code_hash == token_hash(normalized),
                        RecoveryCode.used_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                row.used_at = now
                return True
    return False


@router.post("/login/totp")
async def totp_submit(
    request: Request, rt: Rt, code: Annotated[str, Form(max_length=20)]
) -> Response:
    key = request.cookies.get(PRE_AUTH_COOKIE)
    data = await _redis_get(rt, "preauth", key)
    if data is None:
        return redirect("/login")
    if not await RateLimiter(rt.redis).allow(f"totp:{data['m']}", 10, 600):
        return page("totp.html", 429, error="Хэт олон оролдлого хийлээ. Түр хүлээнэ үү.")
    tenant_id, member_id = uuid.UUID(data["t"]), uuid.UUID(data["m"])
    if not await _check_second_factor(rt, tenant_id, member_id, code):
        return page("totp.html", 401, error="Баталгаажуулах код буруу байна.")
    await _redis_drop(rt, "preauth", key)
    ip = request.client.host if request.client else None
    token = await create_session(rt.sm, tenant_id, member_id, now=utcnow(), ip=ip)
    response = redirect("/")
    set_cookie(response, ADMIN_COOKIE, token)
    response.delete_cookie(PRE_AUTH_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response


@router.post("/logout")
async def logout(ctx: CtxDep) -> Response:
    await end_session(ctx.rt.sm, ctx.sess)
    response = redirect("/login")
    response.delete_cookie(ADMIN_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response


@router.get("/stepup", response_class=HTMLResponse)
async def stepup_form(ctx: CtxDep, next: str = "/") -> HTMLResponse:
    return ctx.render("stepup.html", next=safe_next(next), error=None)


@router.post("/stepup")
async def stepup_submit(
    ctx: CtxDep, code: Annotated[str, Form(max_length=20)], next: Annotated[str, Form()] = "/"
) -> Response:
    if not await RateLimiter(ctx.rt.redis).allow(f"totp:{ctx.member.id}", 10, 600):
        return ctx.render(
            "stepup.html", 429, next=safe_next(next), error="Хэт олон оролдлого хийлээ."
        )
    if not await _check_second_factor(ctx.rt, ctx.tenant_id, ctx.member.id, code):
        return ctx.render(
            "stepup.html", 401, next=safe_next(next), error="Баталгаажуулах код буруу байна."
        )
    await mark_stepped_up(ctx.rt.sm, ctx.sess, utcnow())
    return redirect(safe_next(next))


# ---------------------------------------------------------------- invitations (sign-in roles)


@router.get("/invite", response_class=HTMLResponse)
async def invite_landing() -> HTMLResponse:
    # admin.js copies the token from the URL fragment into this form; it never hits the server
    # in a URL, log line or Referer header.
    return page("invite_start.html", error=None)


@router.post("/invite/start")
async def invite_start(
    request: Request, rt: Rt, token: Annotated[str, Form(min_length=20, max_length=128)]
) -> Response:
    ip = request.client.host if request.client else "unknown"
    if not await RateLimiter(rt.redis).allow(f"invite:{ip}", 20, 600):
        return page("invite_start.html", 429, error="Хэт олон оролдлого хийлээ.")
    try:
        view = await inspect_invite(rt.sm, token, utcnow(), rt.settings.retention_days)
    except InviteError:
        return page("invite_start.html", 400, error=INVITE_INVALID)
    if view.role not in LOGIN_ROLES:
        return page("invite_start.html", 400, error=INVITE_INVALID)
    key = await _redis_put(rt, "invite", {"token": token}, INVITE_TTL)
    response = redirect("/invite/setup")
    set_cookie(response, INVITE_COOKIE, key, INVITE_TTL)
    return response


INVITE_INVALID = (
    "Урилгын холбоос хүчингүй эсвэл хугацаа нь дууссан байна. Админаас шинэ урилга авна уу."
)


async def _invite_state(request: Request, rt: Runtime) -> tuple[str, dict[str, Any], Any] | None:
    key = request.cookies.get(INVITE_COOKIE)
    data = await _redis_get(rt, "invite", key)
    if data is None or key is None:
        return None
    try:
        view = await inspect_invite(rt.sm, data["token"], utcnow(), rt.settings.retention_days)
    except InviteError:
        return None
    return key, data, view


async def _terms_text(rt: Runtime, view: Any) -> str:
    async with tenant_session(rt.sm, view.tenant_id) as s:
        doc = await current_document(s, "tenant_terms")
        return render_document(doc, view.tenant_name, rt.settings.retention_days)


@router.get("/invite/setup", response_class=HTMLResponse)
async def invite_setup_form(request: Request, rt: Rt) -> HTMLResponse:
    state = await _invite_state(request, rt)
    if state is None:
        return page("invite_start.html", 400, error=INVITE_INVALID)
    _, _, view = state
    terms = await _terms_text(rt, view) if view.role == "owner" else None
    return page("invite_setup.html", view=view, terms=terms, error=None)


@router.post("/invite/setup")
async def invite_setup_submit(
    request: Request,
    rt: Rt,
    login_name: Annotated[str, Form(min_length=3, max_length=60, pattern=r"^[A-Za-z0-9._@-]+$")],
    password: Annotated[str, Form(max_length=200)],
    password2: Annotated[str, Form(max_length=200)],
    accept_terms: Annotated[bool, Form()] = False,
    staff_sharing: Annotated[bool, Form()] = False,
    member_consent: Annotated[bool, Form()] = False,
) -> Response:
    state = await _invite_state(request, rt)
    if state is None:
        return page("invite_start.html", 400, error=INVITE_INVALID)
    key, data, view = state
    terms = await _terms_text(rt, view) if view.role == "owner" else None

    def again(error: str) -> HTMLResponse:
        return page("invite_setup.html", 400, view=view, terms=terms, error=error)

    if password != password2:
        return again("Нууц үг хоёр удаа ижил байх ёстой.")
    try:
        check_password(password)
    except WeakSecretError as exc:
        return again(str(exc))
    if view.role == "owner" and not (accept_terms and staff_sharing):
        return again(
            "Үйлчилгээний нөхцөлийг зөвшөөрч, ажилтнуудтай хуваалцах эрхээ баталгаажуулна уу."
        )
    if await rt.resolver.login(login_name) is not None or await _login_taken(rt, view, login_name):
        return again("Энэ нэвтрэх нэр бүртгэлтэй байна. Өөр нэр сонгоно уу.")
    secret = new_totp_secret()
    data.update(
        login_name=login_name,
        password_hash=hash_secret(password),
        totp_secret=secret,
        member_consent=bool(member_consent),
        terms=bool(accept_terms and staff_sharing),
    )
    await rt.redis.set(
        f"khj:invite:{token_hash(key).hex()}",
        rt.crypto.encrypt(json.dumps(data), field_aad(None, "redis", "invite")),
        ex=INVITE_TTL,
    )
    return redirect("/invite/totp")


async def _login_taken(rt: Runtime, view: Any, login_name: str) -> bool:
    async with tenant_session(rt.sm, view.tenant_id) as s:
        n = await s.scalar(
            select(func.count())
            .select_from(Member)
            .where(func.lower(Member.login_name) == login_name.lower())
        )
    return bool(n)


@router.get("/invite/totp", response_class=HTMLResponse)
async def invite_totp_form(request: Request, rt: Rt) -> HTMLResponse:
    state = await _invite_state(request, rt)
    if state is None or "totp_secret" not in state[1]:
        return page("invite_start.html", 400, error=INVITE_INVALID)
    _, data, view = state
    uri = totp_uri(data["totp_secret"], data["login_name"], f"Khugjil ({view.tenant_name})")
    return page("invite_totp.html", view=view, uri=uri, secret=data["totp_secret"], error=None)


@router.post("/invite/totp")
async def invite_totp_submit(
    request: Request, rt: Rt, code: Annotated[str, Form(max_length=20)]
) -> Response:
    state = await _invite_state(request, rt)
    if state is None or "totp_secret" not in state[1]:
        return page("invite_start.html", 400, error=INVITE_INVALID)
    key, data, view = state
    now = utcnow()
    step = verify_totp(data["totp_secret"], code, now, None)
    if step is None:
        uri = totp_uri(data["totp_secret"], data["login_name"], f"Khugjil ({view.tenant_name})")
        return page(
            "invite_totp.html",
            400,
            view=view,
            uri=uri,
            secret=data["totp_secret"],
            error="Код буруу байна. Апп дээрх одоогийн 6 оронтой кодыг оруулна уу.",
        )
    codes = new_recovery_codes()
    try:
        async with tenant_session(rt.sm, view.tenant_id) as s:
            await consume_invite(s, view.invite_id, now)
            member = await s.get(Member, view.member_id, with_for_update=True)
            if member is None or member.status == "revoked":
                raise InviteError("invalid")
            member.login_name = data["login_name"]
            member.password_hash = data["password_hash"]
            member.totp_secret_enc = rt.crypto.encrypt(
                data["totp_secret"], field_aad(view.tenant_id, "member", "totp_secret_enc")
            )
            member.totp_last_step, member.totp_enabled_at, member.status = step, now, "active"
            for c in codes:
                s.add(
                    RecoveryCode(
                        tenant_id=view.tenant_id, member_id=member.id, code_hash=token_hash(c)
                    )
                )
            if view.role == "owner" and data.get("terms"):
                s.add(
                    TenantTermsAcceptance(
                        tenant_id=view.tenant_id,
                        member_id=member.id,
                        terms_version=1,
                        staff_sharing_confirmed=True,
                        accepted_at=now,
                    )
                )
            if data.get("member_consent") and view.role in ("owner", "admin"):
                await give_consent(s, view.tenant_id, member.id, "web", view.consent_version)
    except InviteError:
        return page("invite_start.html", 400, error=INVITE_INVALID)
    await _redis_drop(rt, "invite", key)
    ip = request.client.host if request.client else None
    token = await create_session(rt.sm, view.tenant_id, view.member_id, now=now, ip=ip)
    response = page("invite_done.html", codes=codes)
    set_cookie(response, ADMIN_COOKIE, token)
    response.delete_cookie(INVITE_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response
