"""Admin panel plumbing: templating, authentication context, CSRF, role guards, audit log."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import segno
from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from sqlalchemy.ext.asyncio import AsyncSession

from khugjil.auth.sessions import AdminSession, csrf_token, csrf_valid, ip_prefix, load_session
from khugjil.db.models import AuditLog, Member, Tenant
from khugjil.db.tenancy import tenant_session
from khugjil.delivery.format import format_amount, format_datetime, format_time
from khugjil.runtime import Runtime
from khugjil.web.base import RuntimeHolder

ADMIN_COOKIE = "__Host-khj_adm"
PRE_AUTH_COOKIE = "__Host-khj_pre"
INVITE_COOKIE = "__Host-khj_inv"
TEMPLATES = Path(__file__).resolve().parent / "templates"

ROLE_LABELS = {"owner": "Эзэмшигч", "admin": "Админ", "cashier": "Кассчин", "viewer": "Үзэгч"}
WEEKDAYS = ["Да", "Мя", "Лх", "Пү", "Ба", "Бя", "Ня"]

NAV = [
    ("/", "Тойм", ("owner", "admin", "viewer")),
    ("/accounts", "Дансууд", ("owner", "admin")),
    ("/groups", "Бүлгүүд", ("owner", "admin")),
    ("/members", "Хэрэглэгчид", ("owner", "admin")),
    ("/devices", "Төхөөрөмжүүд", ("owner", "admin")),
    ("/email", "Имэйл холболт", ("owner",)),
    ("/history", "Гүйлгээний түүх", ("owner", "admin", "viewer")),
    ("/settings", "Тохиргоо", ("owner", "admin")),
    ("/audit", "Аудит лог", ("owner", "admin")),
]

env = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=select_autoescape(enabled_extensions=("html",), default=True),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
env.filters["amount"] = lambda v, cur="MNT": format_amount(Decimal(v), cur)
env.filters["dt"] = lambda v: format_datetime(v) if v else "—"
env.filters["hm"] = lambda v: format_time(v) if v else "—"
env.filters["role"] = lambda r: ROLE_LABELS.get(r, r)
env.filters["urlq"] = lambda v: quote(str(v), safe="")
env.globals["WEEKDAYS"] = WEEKDAYS


def utcnow() -> datetime:
    return datetime.now(UTC)


def qr_path(data: str) -> tuple[int, str]:
    """QR code as (size, SVG path data). Rendered inline by templates without |safe."""
    qr = segno.make_qr(data, error="m")
    rows = list(qr.matrix_iter(scale=1, border=2))
    parts = [
        f"M{x} {y}h1v1h-1z" for y, row in enumerate(rows) for x, dark in enumerate(row) if dark
    ]
    return len(rows), "".join(parts)


env.globals["qr_path"] = qr_path


class LoginRequiredError(Exception):
    pass


class StepUpRequiredError(Exception):
    def __init__(self, next_path: str) -> None:
        super().__init__(next_path)
        self.next_path = next_path


@dataclass
class Ctx:
    request: Request
    rt: Runtime
    sess: AdminSession
    member: Member
    tenant: Tenant
    csrf: str

    @property
    def role(self) -> str:
        return self.member.role

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.sess.tenant_id

    def session(self) -> Any:
        return tenant_session(self.rt.sm, self.tenant_id)

    def nav(self) -> list[tuple[str, str]]:
        return [(href, label) for href, label, roles in NAV if self.role in roles]

    def render(self, template: str, status: int = 200, **values: Any) -> HTMLResponse:
        html = env.get_template(template).render(ctx=self, path=self.request.url.path, **values)
        return HTMLResponse(html, status_code=status)

    async def audit(
        self,
        s: AsyncSession,
        action: str,
        *,
        object_type: str | None = None,
        object_id: uuid.UUID | None = None,
        label: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        client = self.request.client.host if self.request.client else None
        s.add(
            AuditLog(
                tenant_id=self.tenant_id,
                actor_kind="member",
                actor_id=self.member.id,
                action=action,
                object_type=object_type,
                object_id=object_id,
                object_label=(label or "")[:120] or None,
                ip_prefix=ip_prefix(client),
                meta=meta or {},
            )
        )


def get_rt(request: Request) -> Runtime:
    holder: RuntimeHolder = request.app.state.holder
    return holder.runtime()


async def admin_ctx(request: Request, rt: Annotated[Runtime, Depends(get_rt)]) -> Ctx:
    token = request.cookies.get(ADMIN_COOKIE)
    sess = await load_session(rt.sm, token, utcnow()) if token else None
    if sess is None:
        raise LoginRequiredError
    async with tenant_session(rt.sm, sess.tenant_id) as s:
        member = await s.get(Member, sess.member_id)
        tenant = await s.get(Tenant, sess.tenant_id)
    if member is None or tenant is None or member.role == "cashier":
        raise LoginRequiredError
    ctx = Ctx(request, rt, sess, member, tenant, csrf_token(rt.crypto, token or ""))
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        presented = request.headers.get("x-csrf-token")
        if presented is None:
            form = await request.form()
            value = form.get("csrf")
            presented = value if isinstance(value, str) else None
        if not csrf_valid(rt.crypto, token or "", presented):
            raise HTTPException(403)
    return ctx


CtxDep = Annotated[Ctx, Depends(admin_ctx)]


def require(*roles: str) -> Callable[[Ctx], Awaitable[Ctx]]:
    async def guard(ctx: CtxDep) -> Ctx:
        if ctx.role not in roles:
            raise HTTPException(403)
        return ctx

    return guard


def require_stepup(ctx: Ctx) -> None:
    if not ctx.sess.stepped_up:
        raise StepUpRequiredError(ctx.request.url.path)


Owner = Annotated[Ctx, Depends(require("owner"))]
Manager = Annotated[Ctx, Depends(require("owner", "admin"))]
Reader = Annotated[Ctx, Depends(require("owner", "admin", "viewer"))]


def redirect(path: str) -> Response:
    if not path.startswith("/") or path.startswith("//"):
        path = "/"
    return RedirectResponse(path, status_code=303)


def safe_next(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def set_cookie(response: Response, name: str, value: str, max_age: int | None = None) -> None:
    response.set_cookie(
        name, value, max_age=max_age, path="/", secure=True, httponly=True, samesite="strict"
    )
