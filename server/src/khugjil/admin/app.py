"""Admin panel (admin.<DOMAIN>). Filled in by the admin-panel step."""

from __future__ import annotations

from fastapi import FastAPI
from starlette.types import ASGIApp

from khugjil.web.base import OriginGuard, RuntimeHolder, SecurityHeaders

ADMIN_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; "
    "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)


def build(holder: RuntimeHolder) -> ASGIApp:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.holder = holder
    origin = f"https://{holder.settings.admin_host}"
    return SecurityHeaders(OriginGuard(app, origin), ADMIN_CSP, no_store_prefixes=("/",))
