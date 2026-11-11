"""In-process HTTP harness for the cashier/admin apps against the test database."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import update

from khugjil.auth.devices import create_pairing
from khugjil.auth.secrets import hash_secret
from khugjil.db.models import Member
from khugjil.db.tenancy import tenant_session
from khugjil.runtime import Runtime
from khugjil.web.app import create_app
from khugjil.web.base import RuntimeHolder

APP = "https://app.khugjil.test"
ADMIN = "https://admin.khugjil.test"
PIN = "482913"
AGENT_VERSION = "0.1.0"


@dataclass
class FakeArq:
    jobs: list[tuple[Any, ...]] = field(default_factory=list)

    async def enqueue_job(self, *args: Any, **kwargs: Any) -> None:
        self.jobs.append(args)

    async def aclose(self) -> None:
        return None


@dataclass
class ApiHarness:
    world: Any
    holder: RuntimeHolder
    app: Any
    client: httpx.AsyncClient
    arq: FakeArq

    @classmethod
    async def create(
        cls, world: Any, dnsfunc: Any, base_url: str = APP, *, agent_devices: bool = False
    ) -> ApiHarness:
        db = world.db
        settings = db.settings.model_copy(update={"agent_devices_enabled": agent_devices})
        arq = FakeArq()
        rt = Runtime(
            settings=settings,
            crypto=db.crypto,
            engine=db.app_engine,
            sm=db.sm,
            redis=db.redis,
            arq=arq,  # type: ignore[arg-type]
            http=httpx.AsyncClient(),
        )
        holder = RuntimeHolder(settings=settings, rt=rt, dnsfunc=dnsfunc)
        app = create_app(holder)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=base_url,
            headers={"origin": base_url},
        )
        return cls(world, holder, app, client, arq)

    @property
    def rt(self) -> Runtime:
        return self.holder.runtime()

    async def close(self) -> None:
        await self.client.aclose()
        await self.rt.http.aclose()

    async def new_pairing(
        self, group_ids: list[uuid.UUID] | None = None, kind: str | None = None
    ) -> Any:
        w = self.world
        extra = {"kind": kind} if kind else {}  # Phase 1 signature has no kind
        async with tenant_session(w.db.sm, w.tenant_id) as s:
            return await create_pairing(
                s,
                w.db.crypto,
                w.tenant_id,
                device_name="Касс-API",
                group_ids=group_ids or [w.group_id],
                created_by=None,
                now=datetime.now(UTC),
                **extra,
            )

    def agent_client(
        self, token: str | None = None, version: str | None = AGENT_VERSION
    ) -> httpx.AsyncClient:
        """A client shaped like the desktop agent's Rust core: no Origin, no cookies, bearer."""
        headers = {"x-khj-agent": version} if version else {}
        if token:
            headers["authorization"] = f"Bearer {token}"
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url=APP, headers=headers
        )

    async def pair_agent(self) -> str:
        pairing = await self.new_pairing(kind="agent")
        async with self.agent_client() as c:
            r = await c.post("/api/v1/pair", json={"token": pairing.token, "client": "agent"})
        assert r.status_code == 200, r.text
        token: str = r.json()["device_token"]
        return token

    async def agent_ready(self) -> str:
        """Paired agent device with an open shift for the world's cashier; returns its token."""
        token = await self.pair_agent()
        await self.set_pin()
        async with self.agent_client(token) as c:
            r = await c.post(
                "/api/v1/shift/start", json={"member_id": str(self.world.member_id), "pin": PIN}
            )
        assert r.status_code == 200, r.text
        return token

    async def pair(self) -> None:
        pairing = await self.new_pairing()
        r = await self.client.post("/api/v1/pair", json={"token": pairing.token})
        assert r.status_code == 200, r.text

    async def set_pin(self, member_id: uuid.UUID | None = None, pin: str = PIN) -> None:
        w = self.world
        async with tenant_session(w.db.sm, w.tenant_id) as s:
            await s.execute(
                update(Member)
                .where(Member.id == (member_id or w.member_id))
                .values(pin_hash=hash_secret(pin))
            )

    async def start_shift(
        self, member_id: uuid.UUID | None = None, pin: str = PIN
    ) -> httpx.Response:
        return await self.client.post(
            "/api/v1/shift/start",
            json={"member_id": str(member_id or self.world.member_id), "pin": pin},
        )

    async def ready(self) -> None:
        """Paired device with an open shift for the world's cashier."""
        await self.pair()
        await self.set_pin()
        r = await self.start_shift()
        assert r.status_code == 200, r.text


async def make_admin(
    world: Any, role: str = "owner", login: str | None = None, password: str = "tsagaan-sar-2026"
) -> tuple[uuid.UUID, str, str]:
    """Create an active member who can sign in. Returns (member_id, login, totp_secret)."""
    import pyotp

    from khugjil.crypto import field_aad

    secret = pyotp.random_base32()
    login = login or f"{role}-{uuid.uuid4().hex[:6]}"
    async with tenant_session(world.db.sm, world.tenant_id) as s:
        m = Member(
            tenant_id=world.tenant_id,
            display_name=f"Д. {role}",
            role=role,
            status="active",
            login_name=login,
            password_hash=hash_secret(password),
            totp_secret_enc=world.db.crypto.encrypt(
                secret, field_aad(world.tenant_id, "member", "totp_secret_enc")
            ),
        )
        s.add(m)
        await s.flush()
        return m.id, login, secret


async def admin_login(
    h: ApiHarness, login: str, secret: str, password: str = "tsagaan-sar-2026"
) -> str:
    """Sign in through the real forms; returns the CSRF token from the page."""
    import re

    import pyotp

    r = await h.client.post("/login", data={"login_name": login, "password": password})
    assert r.status_code == 303, r.text
    r = await h.client.post("/login/totp", data={"code": pyotp.TOTP(secret).now()})
    assert r.status_code == 303, r.text
    page = await h.client.get("/")
    match = re.search(r'name="csrf-token" content="([0-9a-f]+)"', page.text)
    assert match, page.text[:500]
    return match.group(1)
