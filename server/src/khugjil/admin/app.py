"""Admin panel (admin.<DOMAIN>): Jinja + HTMX, desktop-first, responsive."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp

from khugjil.admin.core import LoginRequiredError, StepUpRequiredError, env, redirect
from khugjil.web.base import OriginGuard, RuntimeHolder, SecurityHeaders

ADMIN_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; "
    "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
STATIC = Path(__file__).resolve().parent / "static"

ERRORS = {
    400: ("Хүсэлт буруу байна", "Оруулсан утгуудаа шалгаад дахин оролдоно уу."),
    403: ("Эрх хүрэхгүй байна", "Энэ үйлдлийг хийх эрх танд байхгүй. Эзэмшигчид хандана уу."),
    404: ("Олдсонгүй", "Хуудас эсвэл бичлэг олдсонгүй. Цэснээс дахин сонгоно уу."),
    409: (
        "Үйлдлийг гүйцэтгэх боломжгүй",
        "Энэ үйлдэл одоогийн төлөвтэй зөрчилдөж байна. Мэдээллээ шалгана уу.",
    ),
    429: ("Хэт олон хүсэлт", "Түр хүлээгээд дахин оролдоно уу."),
}


def error_page(status: int) -> HTMLResponse:
    title, hint = ERRORS.get(status, ("Алдаа гарлаа", "Дахин оролдоно уу."))
    html = env.get_template("error.html").render(ctx=None, title=title, hint=hint, status=status)
    return HTMLResponse(html, status_code=status)


def build(holder: RuntimeHolder) -> ASGIApp:
    from khugjil.admin import views_auth, views_pages, views_people

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.holder = holder
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.include_router(views_auth.router)
    app.include_router(views_pages.router)
    app.include_router(views_people.router)

    if holder.settings.dev_routes and holder.settings.env != "production":
        from khugjil.admin import devui

        app.include_router(devui.router)

    @app.exception_handler(LoginRequiredError)
    async def login_required(request: Request, _: Exception) -> Response:
        if request.headers.get("hx-request"):
            return Response(status_code=401, headers={"hx-redirect": "/login"})
        return redirect("/login")

    @app.exception_handler(StepUpRequiredError)
    async def stepup_required(request: Request, exc: Exception) -> Response:
        next_path = exc.next_path if isinstance(exc, StepUpRequiredError) else "/"
        return redirect(f"/stepup?next={next_path}")

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> Response:
        return error_page(exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, __: Any) -> Response:
        return error_page(400)

    origin = f"https://{holder.settings.admin_host}"
    return SecurityHeaders(OriginGuard(app, origin), ADMIN_CSP, no_store_prefixes=("/",))
