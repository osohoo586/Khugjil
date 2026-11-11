"""ASGI plumbing shared by the cashier and admin apps.

* ``HostDispatcher`` routes by Host header to completely separate apps (separate cookies, CSP,
  error handlers); unknown hosts get 421.
* ``SecurityHeaders`` sets strict headers on every response.
* ``OriginGuard`` rejects state-changing requests whose Origin is not the app's own origin
  (CSRF defence in depth on top of SameSite=Strict cookies).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from khugjil.runtime import Runtime
from khugjil.settings import Settings


@dataclass
class RuntimeHolder:
    settings: Settings
    rt: Runtime | None = None
    dnsfunc: Callable[..., Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def runtime(self) -> Runtime:
        if self.rt is None:  # pragma: no cover - lifespan sets it before serving
            raise RuntimeError("runtime not started")
        return self.rt


def _host(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name == b"host":
            host: str = bytes(value).decode("latin-1")
            return host.split(":")[0].lower()
    return ""


async def _plain(send: Send, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": body})


class HostDispatcher:
    def __init__(
        self,
        routes: Mapping[str, ASGIApp],
        on_startup: Callable[[], Awaitable[None]] | None = None,
        on_shutdown: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._routes = {k.lower(): v for k, v in routes.items()}
        self._startup = on_startup
        self._shutdown = on_shutdown

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    if self._startup:
                        await self._startup()
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    if self._shutdown:
                        await self._shutdown()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        app = self._routes.get(_host(scope))
        if app is None:
            if scope["type"] == "http":
                await _plain(send, 421, b"Misdirected Request")
            return
        await app(scope, receive, send)


BASE_HEADERS = {
    "strict-transport-security": "max-age=63072000; includeSubDomains",
    "x-content-type-options": "nosniff",
    # Not "no-referrer": with it, browsers send `Origin: null` on same-site form posts and the
    # OriginGuard refuses every admin form. "same-origin" still sends nothing to other sites.
    "referrer-policy": "same-origin",
    "x-frame-options": "DENY",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


class SecurityHeaders:
    def __init__(self, app: ASGIApp, csp: str, no_store_prefixes: tuple[str, ...] = ()) -> None:
        self.app = app
        self.headers = {**BASE_HEADERS, "content-security-policy": csp}
        self.no_store = no_store_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.decode().lower() not in self.headers
                ]
                headers += [(k.encode(), v.encode()) for k, v in self.headers.items()]
                if any(path.startswith(p) for p in self.no_store):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped)


class OriginGuard:
    """Rejects state-changing requests from other origins.

    With ``allow_non_browser`` (cashier API, only while desktop agents are enabled), a request with
    neither an ``Origin`` nor a ``Cookie`` header also passes: browsers attach ``Origin`` to every
    POST, so such a request comes from a non-browser client (the agent's Rust core) and carries no
    ambient credential that a forged request could abuse.
    """

    UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    def __init__(
        self,
        app: ASGIApp,
        origin: str,
        exempt_prefixes: tuple[str, ...] = (),
        *,
        allow_non_browser: bool = False,
    ) -> None:
        self.app = app
        self.origin = origin.encode()
        self.exempt = exempt_prefixes
        self.allow_non_browser = allow_non_browser

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") in self.UNSAFE
            and not any(scope.get("path", "").startswith(p) for p in self.exempt)
        ):
            headers = dict(scope.get("headers", []))
            origin = headers.get(b"origin")
            non_browser = origin is None and b"cookie" not in headers
            if origin != self.origin and not (self.allow_non_browser and non_browser):
                await _plain(send, 403, b"Forbidden")
                return
        await self.app(scope, receive, send)
