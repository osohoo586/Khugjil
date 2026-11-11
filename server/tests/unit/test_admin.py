"""Admin panel tests (admin.<DOMAIN>)."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pyotp
import pytest
from sqlalchemy import text

from khugjil.db.tenancy import tenant_session
from khugjil.events import RecordingEventBus
from khugjil.services.invites import create_owner_invite
from tests.conftest import make_settings
from tests.fixtures.api import ADMIN, ApiHarness, admin_login, make_admin
from tests.fixtures.emails import FakeTxn, Mailer

pytestmark = pytest.mark.db


@pytest.fixture
async def admin(world: Any, mailer: Mailer) -> AsyncIterator[ApiHarness]:
    h = await ApiHarness.create(world, mailer.dns, base_url=ADMIN)
    try:
        yield h
    finally:
        await h.close()


@pytest.fixture
async def owner(admin: ApiHarness, world: Any) -> tuple[ApiHarness, str, uuid.UUID]:
    member_id, login, secret = await make_admin(world, "owner")
    csrf = await admin_login(admin, login, secret)
    admin.extra = {"secret": secret}  # type: ignore[attr-defined]
    return admin, csrf, member_id


async def stepup(h: ApiHarness, csrf: str, secret: str, next_path: str = "/") -> None:
    import asyncio

    await asyncio.sleep(0)  # a fresh TOTP step is needed: the login already used the current one
    totp = pyotp.TOTP(secret)
    for offset in (30, -30):
        code = totp.at(int(__import__("time").time()) + offset)
        r = await h.client.post("/stepup", data={"csrf": csrf, "code": code, "next": next_path})
        if r.status_code == 303:
            return
    raise AssertionError("step-up failed")


# ---------------------------------------------------------------- sign-in


async def test_login_page_and_security_headers(admin: ApiHarness) -> None:
    r = await admin.client.get("/login")
    assert r.status_code == 200
    assert "Нэвтрэх" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "unsafe" not in csp
    assert r.headers["cache-control"] == "no-store"


async def test_unauthenticated_redirects_to_login(admin: ApiHarness) -> None:
    r = await admin.client.get("/accounts")
    assert (r.status_code, r.headers["location"]) == (303, "/login")
    r = await admin.client.get("/accounts", headers={"hx-request": "true"})
    assert r.status_code == 401
    assert r.headers["hx-redirect"] == "/login"


async def test_wrong_password_locks(admin: ApiHarness, world: Any) -> None:
    _, login, _ = await make_admin(world, "admin")
    codes = [
        (
            await admin.client.post("/login", data={"login_name": login, "password": "x" * 12})
        ).status_code
        for _ in range(5)
    ]
    assert codes == [401] * 5
    r = await admin.client.post(
        "/login", data={"login_name": login, "password": "tsagaan-sar-2026"}
    )
    assert r.status_code == 423
    unknown = await admin.client.post("/login", data={"login_name": "nobody", "password": "x" * 12})
    assert unknown.status_code == 401


async def test_totp_required_and_replay_rejected(admin: ApiHarness, world: Any) -> None:
    _, login, secret = await make_admin(world, "owner")
    await admin.client.post("/login", data={"login_name": login, "password": "tsagaan-sar-2026"})
    assert (await admin.client.get("/")).status_code == 303  # no session before TOTP
    code = pyotp.TOTP(secret).now()
    bad = await admin.client.post("/login/totp", data={"code": "000000"})
    assert bad.status_code == 401
    assert (await admin.client.post("/login/totp", data={"code": code})).status_code == 303
    admin.client.cookies.clear()
    await admin.client.post("/login", data={"login_name": login, "password": "tsagaan-sar-2026"})
    assert (await admin.client.post("/login/totp", data={"code": code})).status_code == 401
    assert (await admin.client.post("/login/totp", data={"code": "1"})).status_code == 401
    admin.client.cookies.clear()
    assert (await admin.client.post("/login/totp", data={"code": code})).status_code == 303
    assert (await admin.client.get("/")).headers.get("location") == "/login"


async def test_cashier_cannot_sign_in(admin: ApiHarness, world: Any) -> None:
    _, login, _ = await make_admin(world, "cashier")
    r = await admin.client.post(
        "/login", data={"login_name": login, "password": "tsagaan-sar-2026"}
    )
    assert r.status_code == 401


async def test_csrf_and_origin_required(owner: tuple[ApiHarness, str, uuid.UUID]) -> None:
    h, csrf, _ = owner
    assert (await h.client.post("/groups", data={"name": "X"})).status_code == 403
    r = await h.client.post(
        "/groups", data={"name": "X", "csrf": csrf}, headers={"origin": "https://evil.example"}
    )
    assert r.status_code == 403
    r = await h.client.post("/groups", data={"name": "X", "csrf": csrf})
    assert r.status_code == 303


async def test_logout(owner: tuple[ApiHarness, str, uuid.UUID]) -> None:
    h, csrf, _ = owner
    assert (await h.client.post("/logout", data={"csrf": csrf})).status_code == 303
    assert (await h.client.get("/")).headers["location"] == "/login"


# ---------------------------------------------------------------- roles


async def test_viewer_role_matrix(admin: ApiHarness, world: Any) -> None:
    _, login, secret = await make_admin(world, "viewer")
    await admin_login(admin, login, secret)
    for path, expected in (
        ("/", 200),
        ("/history", 200),
        ("/accounts", 403),
        ("/members", 403),
        ("/email", 403),
        ("/settings", 403),
        ("/audit", 403),
        ("/history.csv", 403),
    ):
        assert (await admin.client.get(path)).status_code == expected, path


async def test_admin_cannot_manage_owners_or_email(admin: ApiHarness, world: Any) -> None:
    _, login, secret = await make_admin(world, "admin")
    csrf = await admin_login(admin, login, secret)
    assert (await admin.client.get("/email")).status_code == 403
    r = await admin.client.post(
        "/members", data={"csrf": csrf, "display_name": "X", "role": "owner"}
    )
    assert r.status_code == 403
    page = (await admin.client.get("/members")).text
    assert 'value="owner"' not in page


# ---------------------------------------------------------------- pages


async def test_every_page_renders(
    owner: tuple[ApiHarness, str, uuid.UUID], world: Any, mailer: Mailer
) -> None:
    h, _, _ = owner
    await world.ingest(mailer.bank_mail(FakeTxn(description='=HYPERLINK("x")')))
    for path in (
        "/",
        "/accounts",
        "/groups",
        f"/groups/{world.group_id}",
        "/members",
        f"/members/{world.member_id}",
        "/devices",
        f"/devices/{world.device_id}",
        "/email",
        "/email/status",
        "/history",
        "/settings",
        "/audit",
    ):
        r = await h.client.get(path)
        assert r.status_code == 200, (path, r.text[:300])
        assert "<script>" not in r.text
    history = (await h.client.get("/history")).text
    assert "2,800 ₮" in history
    assert "Төв дэлгүүр ••2222" in history


async def test_accounts_crud_and_audit(
    owner: tuple[ApiHarness, str, uuid.UUID], world: Any
) -> None:
    h, csrf, _ = owner
    r = await h.client.post(
        "/accounts", data={"csrf": csrf, "number": "5555 5555 55", "alias": "Салбар-3"}
    )
    assert r.status_code == 303
    dup = await h.client.post(
        "/accounts", data={"csrf": csrf, "number": "5555555555", "alias": "Давхар"}
    )
    assert "бүртгэлтэй" in dup.text
    bad = await h.client.post("/accounts", data={"csrf": csrf, "number": "12ab", "alias": "Буруу"})
    assert "6-20" in bad.text
    acc = next(a for a in await world.rows("bank_account") if a["alias"] == "Салбар-3")
    r = await h.client.post(
        f"/accounts/{acc['id']}", data={"csrf": csrf, "alias": "Салбар-3Б", "status": "inactive"}
    )
    assert r.status_code == 303
    page = (await h.client.get("/accounts")).text
    assert "Салбар-3Б" in page
    assert "5555555555" not in page
    actions = [a["action"] for a in await world.rows("audit_log")]
    assert "account.add" in actions
    assert "account.inactive" in actions


async def test_groups_link_members_devices(
    owner: tuple[ApiHarness, str, uuid.UUID], world: Any
) -> None:
    h, csrf, _ = owner
    r = await h.client.post("/groups", data={"csrf": csrf, "name": "Салбар-2"})
    group_id = r.headers["location"].rsplit("/", 1)[1]
    data = {
        "csrf": csrf,
        "name": "Салбар-2",
        "status": "active",
        "account_ids": [str(world.account_id)],
        "member_ids": [str(world.member_id)],
        "device_ids": [str(world.device_id)],
    }
    assert (await h.client.post(f"/groups/{group_id}", data=data)).status_code == 303
    links = [r for r in await world.rows("group_member") if str(r["group_id"]) == group_id]
    assert len(links) == 1
    bogus = {**data, "account_ids": [str(uuid.uuid4())]}
    assert (await h.client.post(f"/groups/{group_id}", data=bogus)).status_code == 404


async def test_member_invite_reinvite_schedule_revoke(
    owner: tuple[ApiHarness, str, uuid.UUID], world: Any
) -> None:
    h, csrf, owner_id = owner
    r = await h.client.post(
        "/members",
        data={
            "csrf": csrf,
            "display_name": "Г. Туяа",
            "role": "cashier",
            "group_ids": [str(world.group_id)],
        },
    )
    assert r.status_code == 200
    assert "https://app.khugjil.test/invite#t=" in r.text
    assert "<svg" in r.text
    r = await h.client.post(
        "/members", data={"csrf": csrf, "display_name": "Н. Оюун", "role": "viewer"}
    )
    assert "https://admin.khugjil.test/invite#t=" in r.text
    mid = world.member_id
    r = await h.client.post(
        f"/members/{mid}/schedule",
        data={"csrf": csrf, "weekday": 0, "start": "09:00", "end": "18:00"},
    )
    assert r.status_code == 303
    (window,) = await world.rows("duty_window")
    bad = await h.client.post(
        f"/members/{mid}/schedule",
        data={"csrf": csrf, "weekday": 0, "start": "18:00", "end": "09:00"},
    )
    assert bad.status_code == 400
    assert (
        await h.client.post(f"/members/{mid}/schedule/{window['id']}/delete", data={"csrf": csrf})
    ).status_code == 303
    r = await h.client.post(f"/members/{mid}/reinvite", data={"csrf": csrf})
    assert "invite#t=" in r.text
    assert (
        await h.client.post(f"/members/{owner_id}/revoke", data={"csrf": csrf})
    ).status_code == 409
    assert (await h.client.post(f"/members/{mid}/revoke", data={"csrf": csrf})).status_code == 303
    member = next(m for m in await world.rows("member") if m["id"] == mid)
    assert member["status"] == "revoked"
    assert all(s["ended_at"] for s in await world.rows("shift"))


async def test_last_owner_cannot_be_demoted(owner: tuple[ApiHarness, str, uuid.UUID]) -> None:
    h, csrf, owner_id = owner
    r = await h.client.post(
        f"/members/{owner_id}", data={"csrf": csrf, "display_name": "X", "role": "admin"}
    )
    assert r.status_code == 409


async def test_device_pairing_and_revoke(
    owner: tuple[ApiHarness, str, uuid.UUID], world: Any
) -> None:
    h, csrf, _ = owner
    data = {"csrf": csrf, "device_name": "Касс-3", "group_ids": [str(world.group_id)]}
    plain = await h.client.post("/devices/pair", data=data)
    assert plain.status_code == 200
    assert re.search(r"[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}", plain.text)
    hx = await h.client.post(
        "/devices/pair", data=data, headers={"hx-request": "true", "x-csrf-token": csrf}
    )
    assert "<html" not in hx.text
    assert "app.khugjil.test/pair" in hx.text
    assert "#t=" not in hx.text  # the token is only inside the QR code
    bus = RecordingEventBus()
    h.rt.__class__.bus = property(lambda self: bus)  # type: ignore[method-assign]
    try:
        r = await h.client.post(f"/devices/{world.device_id}/revoke", data={"csrf": csrf})
    finally:
        del h.rt.__class__.bus
        from khugjil.events import RedisEventBus

        h.rt.__class__.bus = property(lambda self: RedisEventBus(self.redis))  # type: ignore[method-assign]
    assert r.status_code == 303
    assert (world.device_id, {"type": "device_revoked"}) in bus.events
    assert (await h.client.get(f"/devices/{world.device_id}")).status_code == 404


async def test_email_rotate_requires_stepup(
    owner: tuple[ApiHarness, str, uuid.UUID], world: Any
) -> None:
    h, csrf, _ = owner
    page = (await h.client.get("/email")).text
    assert world.inbound_address in page
    async with tenant_session(world.db.sm, world.tenant_id) as s:
        await s.execute(text("UPDATE session SET stepup_at = now() - interval '1 hour'"))
    r = await h.client.post("/email/rotate", data={"csrf": csrf})
    assert r.headers["location"].startswith("/stepup?next=/email/rotate")
    await stepup(h, csrf, h.extra["secret"], "/email")  # type: ignore[attr-defined]
    r = await h.client.post("/email/rotate", data={"csrf": csrf})
    assert r.status_code == 303
    assert world.inbound_address not in (await h.client.get("/email")).text
    assert (await h.client.post("/email/test", data={"csrf": csrf})).status_code == 303


async def test_history_csv_is_formula_safe(
    owner: tuple[ApiHarness, str, uuid.UUID], world: Any, mailer: Mailer
) -> None:
    h, _, _ = owner
    await world.ingest(mailer.bank_mail(FakeTxn(description="=cmd|' /C calc'!A0")))
    r = await h.client.get("/history.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "'=cmd" in r.text
    assert "\n=cmd" not in r.text
    assert "history.export_csv" in [a["action"] for a in await world.rows("audit_log")]
    filtered = await h.client.get("/history", params={"amount_min": "5000"})
    assert "2,800 ₮" not in filtered.text
    assert (
        await h.client.get("/history", params={"date_from": "bad", "page": "-3"})
    ).status_code == 200


async def test_settings_save_and_validation(
    owner: tuple[ApiHarness, str, uuid.UUID], world: Any
) -> None:
    h, csrf, _ = owner
    base = {
        "csrf": csrf,
        "accent": "navy",
        "toast_seconds": "10",
        "health_silence_hours": "6",
        "shift_idle_hours": "8",
        "business_start": "08:00",
        "business_end": "20:00",
        "business_days": ["0", "1", "2"],
        "voice_enabled": "true",
        "voice_max_amount": "50,000",
    }
    assert (await h.client.post("/settings", data=base)).status_code == 303
    (tenant,) = await world.app_rows("tenant")
    assert tenant["accent"] == "navy"
    assert str(tenant["voice_max_amount"]) == "50000.00"
    bad = await h.client.post("/settings", data={**base, "business_start": "21:00"})
    assert "шалгана уу" in bad.text
    out_of_range = await h.client.post("/settings", data={**base, "toast_seconds": "99"})
    assert out_of_range.status_code == 400
    assert 'data-accent="navy"' in (await h.client.get("/settings")).text


async def test_invite_flow_for_owner(admin: ApiHarness, world: Any) -> None:
    link = await create_owner_invite(admin.rt, world.tenant_id, "Шинэ эзэмшигч")
    token = link.split("#t=")[1]
    assert (await admin.client.get("/invite")).status_code == 200
    r = await admin.client.post("/invite/start", data={"token": token})
    assert r.headers["location"] == "/invite/setup"
    setup = await admin.client.get("/invite/setup")
    assert "Үйлчилгээний нөхцөл" in setup.text
    form = {
        "login_name": "shine.ezen",
        "password": "uul-us-salhi-2026",
        "password2": "uul-us-salhi-2026",
    }
    assert "Үйлчилгээний нөхцөлийг" in (await admin.client.post("/invite/setup", data=form)).text
    form.update(accept_terms="true", staff_sharing="true", member_consent="true")
    assert (await admin.client.post("/invite/setup", data=form)).headers[
        "location"
    ] == "/invite/totp"
    page = (await admin.client.get("/invite/totp")).text
    secret = re.search(r"<code class=\"tabular\">([A-Z2-7]+)</code>", page)
    assert secret
    done = await admin.client.post("/invite/totp", data={"code": pyotp.TOTP(secret.group(1)).now()})
    assert done.status_code == 200
    codes = re.findall(r"<li>([0-9A-Z]{5}-[0-9A-Z]{5})</li>", done.text)
    assert len(codes) == 10
    assert (await admin.client.get("/")).status_code == 200
    assert await world.count("tenant_terms_acceptance") == 1
    again = await admin.client.post("/invite/start", data={"token": token})
    assert "хүчингүй" in again.text


async def test_recovery_code_login(admin: ApiHarness, world: Any) -> None:
    link = await create_owner_invite(admin.rt, world.tenant_id)
    await admin.client.post("/invite/start", data={"token": link.split("#t=")[1]})
    form = {
        "login_name": "nooots",
        "password": "uul-us-salhi-2026",
        "password2": "uul-us-salhi-2026",
        "accept_terms": "true",
        "staff_sharing": "true",
    }
    await admin.client.post("/invite/setup", data=form)
    page = (await admin.client.get("/invite/totp")).text
    secret = re.search(r"<code class=\"tabular\">([A-Z2-7]+)</code>", page)
    assert secret
    done = await admin.client.post("/invite/totp", data={"code": pyotp.TOTP(secret.group(1)).now()})
    code = re.findall(r"<li>([0-9A-Z]{5}-[0-9A-Z]{5})</li>", done.text)[0]
    admin.client.cookies.clear()
    await admin.client.post(
        "/login", data={"login_name": "nooots", "password": "uul-us-salhi-2026"}
    )
    assert (await admin.client.post("/login/totp", data={"code": code.lower()})).status_code == 303
    admin.client.cookies.clear()
    await admin.client.post(
        "/login", data={"login_name": "nooots", "password": "uul-us-salhi-2026"}
    )
    assert (await admin.client.post("/login/totp", data={"code": code})).status_code == 401


async def test_dev_ui_only_when_enabled(admin: ApiHarness, world: Any, mailer: Mailer) -> None:
    assert (await admin.client.get("/dev/ui")).status_code in (303, 404)
    from khugjil.web.app import create_app
    from khugjil.web.base import RuntimeHolder

    dev = RuntimeHolder(settings=make_settings(dev_routes=True), rt=admin.rt, dnsfunc=mailer.dns)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(dev)), base_url=ADMIN
    ) as c:
        r = await c.get("/dev/ui")
    assert r.status_code == 200
    assert "Компонентуудын жагсаалт" in r.text


async def test_agent_pairing_kind_only_when_enabled(
    owner: tuple[ApiHarness, str, uuid.UUID], world: Any, mailer: Mailer
) -> None:
    h, csrf, _ = owner
    assert 'name="kind"' not in (await h.client.get("/devices")).text
    data = {"csrf": csrf, "device_name": "Касс-4", "group_ids": [str(world.group_id)]}
    assert (await h.client.post("/devices/pair", data={**data, "kind": "agent"})).status_code == 400

    on = await ApiHarness.create(world, mailer.dns, base_url=ADMIN, agent_devices=True)
    try:
        member_id, login, secret = await make_admin(world, "owner")
        csrf_on = await admin_login(on, login, secret)
        assert 'name="kind" value="agent"' in (await on.client.get("/devices")).text
        r = await on.client.post(
            "/devices/pair",
            data={"csrf": csrf_on, "device_name": "Касс-4", "group_ids": [str(world.group_id)],
                  "kind": "agent"},
        )  # fmt: skip
        assert r.status_code == 200
        assert re.search(r"[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}", r.text)
        assert "/pair#t=" not in r.text  # no QR link for agents
        assert "<svg" not in r.text
        assert "касс програм" in r.text
        codes = await world.rows("pairing_code")
        assert [c["kind"] for c in codes] == ["agent"]
        audit = [a for a in await world.rows("audit_log") if a["action"] == "device.pairing_code"]
        assert audit[-1]["meta"] == {"kind": "agent"}
        assert member_id
    finally:
        await on.close()


async def test_referrer_policy_keeps_same_origin_form_posts_working(admin: ApiHarness) -> None:
    """With `no-referrer`, browsers send `Origin: null` on same-origin form posts (Fetch spec,
    "serializing a request origin"), and the Origin guard refuses them: the login form broke in
    real browsers. `same-origin` sends the real origin to us and no referrer to anyone else."""
    r = await admin.client.get("/login")
    assert r.headers["referrer-policy"] == "same-origin"
    data = {"login_name": "nobody", "password": "x" * 12}
    refused = await admin.client.post("/login", data=data, headers={"origin": "null"})
    assert refused.status_code == 403  # the guard itself stays strict
    allowed = await admin.client.post("/login", data=data)
    assert allowed.status_code != 403
