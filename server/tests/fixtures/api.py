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
    async def create(cls, world: Any, dnsfunc: Any, base_url: str = APP) -> ApiHarness:
        db = world.db
        arq = FakeArq()
        rt = Runtime(
            settings=db.settings,
            crypto=db.crypto,
            engine=db.app_engine,
            sm=db.sm,
            redis=db.redis,
            arq=arq,  # type: ignore[arg-type]
            http=httpx.AsyncClient(),
        )
        holder = RuntimeHolder(settings=db.settings, rt=rt, dnsfunc=dnsfunc)
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

    async def new_pairing(self, group_ids: list[uuid.UUID] | None = None) -> Any:
        w = self.world
        async with tenant_session(w.db.sm, w.tenant_id) as s:
            return await create_pairing(
                s,
                w.db.crypto,
                w.tenant_id,
                device_name="Касс-API",
                group_ids=group_ids or [w.group_id],
                created_by=None,
                now=datetime.now(UTC),
            )

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
