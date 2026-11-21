"""Remembered browsers (owner's request, 2026-11-21): after a TOTP sign-in a browser may skip
the code for 30 days. The password is always asked, step-up always asks for the code, and
re-inviting or revoking the member forgets every browser."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pyotp
import pytest
from sqlalchemy import text, update

from khugjil.auth.trusted import TRUST_COOKIE, device_label
from khugjil.crypto import token_hash
from khugjil.db.models import TrustedDevice
from khugjil.db.tenancy import tenant_session
from tests.fixtures.api import ADMIN, ApiHarness, make_admin
from tests.fixtures.emails import Mailer

pytestmark = pytest.mark.db
PASSWORD = "tsagaan-sar-2026"
CHROME_WIN = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


@pytest.fixture
async def h(world: Any, mailer: Mailer) -> AsyncIterator[ApiHarness]:
    harness = await ApiHarness.create(world, mailer.dns, base_url=ADMIN)
    try:
        yield harness
    finally:
        await harness.close()


async def _password(h: ApiHarness, login: str) -> Any:
    return await h.client.post(
        "/login",
        data={"login_name": login, "password": PASSWORD},
        headers={"user-agent": CHROME_WIN},
    )


async def _full_login(h: ApiHarness, login: str, secret: str, remember: bool, step: int = 0) -> Any:
    """step=1 uses the next TOTP step: a step already used by this member is refused."""
    r = await _password(h, login)
    assert r.headers["location"] == "/login/totp"
    data = {"code": pyotp.TOTP(secret).at(int(time.time()) + 30 * step)}
    if remember:
        data["remember_device"] = "true"
    return await h.client.post("/login/totp", data=data, headers={"user-agent": CHROME_WIN})


def _csrf(page: str) -> str:
    match = re.search(r'name="csrf-token" content="([0-9a-f]+)"', page)
    assert match
    return match.group(1)


async def _devices(world: Any) -> list[dict[str, Any]]:
    return [r for r in await world.rows("trusted_device") if r["tenant_id"] == world.tenant_id]


async def test_remembered_browser_skips_only_the_code(h: ApiHarness, world: Any) -> None:
    member_id, login, secret = await make_admin(world, "owner")
    r = await _full_login(h, login, secret, remember=True)
    assert r.status_code == 303
    cookie = r.headers.get_list("set-cookie")
    trust = next(c for c in cookie if c.startswith(f"{TRUST_COOKIE}="))
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/", "Max-Age=2592000"):
        assert flag.lower() in trust.lower(), flag
    (row,) = await _devices(world)
    assert row["member_id"] == member_id
    assert row["label"] == "Chrome, Windows"
    value = trust.split(";")[0].split("=", 1)[1]
    assert row["token_hash"] == token_hash(value)  # only the hash is stored
    assert value.encode() not in repr(await world.rows("trusted_device")).encode()

    # Sign out and back in: the password alone opens a session, without the code.
    page = (await h.client.get("/")).text
    await h.client.post("/logout", data={"csrf": _csrf(page)})
    r = await _password(h, login)
    assert (r.status_code, r.headers["location"]) == (303, "/")
    page = (await h.client.get("/")).text
    assert "<h1>Тойм</h1>" in page

    # ...but the session is not stepped up: a sensitive action still asks for the code.
    r = await h.client.post("/email/addresses", data={"csrf": _csrf(page), "label": "x"})
    assert r.headers["location"] == "/stepup?next=/email"

    # A wrong password is still refused, remembered browser or not.
    await h.client.post("/logout", data={"csrf": _csrf(page)})
    r = await h.client.post("/login", data={"login_name": login, "password": "wrong-pass-123"})
    assert r.status_code == 401
    actions = [r["action"] for r in await world.rows("audit_log")]
    assert "trusted.remember" in actions
    assert actions.count("login.remembered") == 1  # each code-less sign-in is on record


async def test_without_the_tick_nothing_is_remembered(h: ApiHarness, world: Any) -> None:
    _, login, secret = await make_admin(world, "admin")
    r = await _full_login(h, login, secret, remember=False)
    assert r.status_code == 303
    assert not any(c.startswith(TRUST_COOKIE) for c in r.headers.get_list("set-cookie"))
    assert await _devices(world) == []
    page = (await h.client.get("/")).text
    await h.client.post("/logout", data={"csrf": _csrf(page)})
    assert (await _password(h, login)).headers["location"] == "/login/totp"


async def test_cookie_is_bound_to_member_and_expires(h: ApiHarness, world: Any) -> None:
    _, login_a, secret_a = await make_admin(world, "owner")
    _, login_b, _ = await make_admin(world, "admin")
    await _full_login(h, login_a, secret_a, remember=True)
    page = (await h.client.get("/")).text
    await h.client.post("/logout", data={"csrf": _csrf(page)})
    # Another member on the same browser still needs their own code.
    assert (await _password(h, login_b)).headers["location"] == "/login/totp"
    # After 30 days the browser is no longer remembered.
    async with tenant_session(world.db.sm, world.tenant_id) as s:
        await s.execute(
            update(TrustedDevice).values(expires_at=TrustedDevice.expires_at - timedelta(days=31))
        )
    assert (await _password(h, login_a)).headers["location"] == "/login/totp"


async def test_forget_and_reinvite_forget_the_browser(h: ApiHarness, world: Any) -> None:
    owner_id, login, secret = await make_admin(world, "owner")
    _, admin_login_name, admin_secret = await make_admin(world, "admin")
    await _full_login(h, admin_login_name, admin_secret, remember=True)
    admin_page = (await h.client.get("/me/devices")).text
    assert "Chrome, Windows" in admin_page
    assert "Энэ төхөөрөмж" in admin_page
    await h.client.post("/logout", data={"csrf": _csrf(admin_page)})

    # The owner remembers this browser too, then forgets it from the page.
    await _full_login(h, login, secret, remember=True)
    page = (await h.client.get("/me/devices")).text
    mine = [d for d in await _devices(world) if d["member_id"] == owner_id]
    assert len(mine) == 1
    other = uuid.uuid4()
    r = await h.client.post(f"/me/devices/{other}/forget", data={"csrf": _csrf(page)})
    assert r.status_code == 404
    admin_row = next(d for d in await _devices(world) if d["member_id"] != owner_id)
    r = await h.client.post(f"/me/devices/{admin_row['id']}/forget", data={"csrf": _csrf(page)})
    assert r.status_code == 404  # someone else's browser is not mine to forget
    r = await h.client.post(f"/me/devices/{mine[0]['id']}/forget", data={"csrf": _csrf(page)})
    assert r.headers["location"] == "/me/devices?notice=forgotten"
    assert [d["member_id"] for d in await _devices(world)] == [admin_row["member_id"]]

    # Re-inviting the admin forgets every browser remembered for them.
    page = (await h.client.get("/members")).text
    r = await h.client.post(
        f"/members/{admin_row['member_id']}/reinvite", data={"csrf": _csrf(page)}
    )
    assert r.status_code == 200
    assert await _devices(world) == []

    # "Forget all" clears the cookie too.
    page = (await h.client.get("/")).text
    await h.client.post("/logout", data={"csrf": _csrf(page)})
    await _full_login(h, login, secret, remember=True, step=1)
    page = (await h.client.get("/me/devices")).text
    r = await h.client.post("/me/devices/forget", data={"csrf": _csrf(page)})
    assert any(c.startswith(f'{TRUST_COOKIE}=""') for c in r.headers.get_list("set-cookie"))
    assert await _devices(world) == []
    actions = [r["action"] for r in await world.rows("audit_log")]
    assert actions.count("trusted.forget") == 2


async def test_trusted_devices_are_tenant_scoped(world: Any, other_world: Any) -> None:
    """SPEC §3.9: another tenant's remembered browser is invisible (RLS), so its cookie can never
    skip the code for a member of this tenant."""
    from datetime import UTC, datetime

    from khugjil.auth.trusted import recognise, remember

    member_b, _, _ = await make_admin(other_world, "owner")
    now = datetime.now(UTC)
    async with tenant_session(other_world.db.sm, other_world.tenant_id) as s:
        cookie, _ = await remember(
            s, other_world.tenant_id, member_b, now=now, user_agent=None, ip=None
        )
    async with tenant_session(world.db.sm, world.tenant_id) as s:
        assert (await s.execute(text("SELECT count(*) FROM trusted_device"))).scalar_one() == 0
        assert not await recognise(s, member_b, cookie, now)


def test_device_label() -> None:
    assert device_label(CHROME_WIN) == "Chrome, Windows"
    mac_safari = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.0 Safari/605.1.15"
    )
    assert device_label(mac_safari) == "Safari, macOS"
    edge = CHROME_WIN + " Edg/130.0.0.0"
    assert device_label(edge) == "Edge, Windows"
    assert device_label(None) == "Хөтөч"
    assert device_label("<script>") == "Хөтөч"
