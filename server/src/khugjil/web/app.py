"""ASGI entry point: ``uvicorn khugjil.web.app:production_app --factory``.

Two independent apps behind a Host dispatcher:
  app.<DOMAIN>    cashier JSON API v1 + SSE + server-to-server hooks (static bundle via Caddy)
  admin.<DOMAIN>  admin panel (Jinja + HTMX)
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp

from khugjil.runtime import Runtime
from khugjil.settings import Settings
from khugjil.web.base import HostDispatcher, OriginGuard, RuntimeHolder, SecurityHeaders

CASHIER_API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"


def build_cashier_app(holder: RuntimeHolder) -> ASGIApp:
    from khugjil.api_v1 import cashier, hooks, stream  # noqa: F401 - stream registers routes

    app = FastAPI(
        title="Khugjil cashier API",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
    )
    app.state.holder = holder
    app.include_router(cashier.router)
    app.include_router(hooks.router)

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        detail: Any = exc.detail if isinstance(exc.detail, dict) else {"error": "error"}
        return JSONResponse(detail, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {"error": "bad_request", "message": cashier.MESSAGES["bad_request"]}, status_code=400
        )

    origin = f"https://{holder.settings.app_host}"
    guarded = OriginGuard(app, origin, exempt_prefixes=("/hooks/",))
    return SecurityHeaders(guarded, CASHIER_API_CSP, no_store_prefixes=("/api/",))


def build_admin_app(holder: RuntimeHolder) -> ASGIApp:
    from khugjil.admin.app import build

    return build(holder)


def create_app(holder: RuntimeHolder) -> HostDispatcher:
    settings = holder.settings

    async def startup() -> None:
        if holder.rt is None:
            holder.rt = await Runtime.create(settings)

    async def shutdown() -> None:
        if holder.rt is not None:
            await holder.rt.close()

    return HostDispatcher(
        {
            settings.app_host: build_cashier_app(holder),
            settings.admin_host: build_admin_app(holder),
        },
        on_startup=startup,
        on_shutdown=shutdown,
    )


def production_app() -> HostDispatcher:
    from dkim.dnsplug import get_txt

    from khugjil.logs import configure_logging

    configure_logging()
    return create_app(RuntimeHolder(settings=Settings(), dnsfunc=get_txt))
