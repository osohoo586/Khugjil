"""Phase 2 desktop agent (PLAN-PHASE2 §2, §5): device auth for `kind = agent` keeps tenant
isolation (SPEC §3.9) and consent (§3.10) exactly as for web devices.

- The agent token travels only as `Authorization: Bearer`, the web token only as the
  `__Host-khj_dev` cookie; neither works the other way (S7).
- A bearer request that carries an `Origin` header is refused (S6).
- Pairing codes are bound to their kind; a wrong-kind redemption does not consume the code.
- Cookie-authenticated requests still need the same-origin `Origin` header (CSRF).
- With the feature flag off, none of this is reachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from tests.fixtures.api import APP, PIN, ApiHarness
from tests.fixtures.emails import FakeTxn, Mailer

pytestmark = [pytest.mark.invariant(9), pytest.mark.db]

DEVICE_COOKIE = "__Host-khj_dev"


@pytest.fixture
async def api(world: Any, mailer: Mailer) -> AsyncIterator[ApiHarness]:
    h = await ApiHarness.create(world, mailer.dns, agent_devices=True)
    try:
        yield h
    finally:
        await h.close()


async def _agent_device(world: Any) -> dict[str, Any]:
    return next(d for d in await world.app_rows("device") if d["name"] == "Касс-API")


# ---------------------------------------------------------------- pairing


async def test_agent_pairing_returns_token_in_body_and_no_cookie(
    api: ApiHarness, world: Any
) -> None:
    pairing = await api.new_pairing(kind="agent")
    async with api.agent_client() as c:
        r = await c.post("/api/v1/pair", json={"token": pairing.token, "client": "agent"})
    assert r.status_code == 200, r.text
    assert "set-cookie" not in r.headers
    token = r.json()["device_token"]
    assert len(token) >= 43  # 256 bits, base64url
    assert (await _agent_device(world))["kind"] == "agent"
    async with api.agent_client(token) as c:
        info = await c.get("/api/v1/device")
    assert info.status_code == 200
    assert info.json()["device_name"] == "Касс-API"


async def test_web_pairing_never_returns_a_token_in_the_body(api: ApiHarness) -> None:
    pairing = await api.new_pairing(kind="web")
    r = await api.client.post("/api/v1/pair", json={"token": pairing.token})
    assert r.status_code == 200
    assert "device_token" not in r.json()
    assert f"{DEVICE_COOKIE}=" in r.headers["set-cookie"]


async def test_pairing_code_is_bound_to_its_kind(api: ApiHarness) -> None:
    web, agent = await api.new_pairing(kind="web"), await api.new_pairing(kind="agent")
    async with api.agent_client() as c:
        wrong = await c.post("/api/v1/pair", json={"token": web.token, "client": "agent"})
    assert wrong.status_code == 400
    assert wrong.json()["error"] == "invalid_code"
    wrong = await api.client.post("/api/v1/pair", json={"token": agent.token})
    assert wrong.status_code == 400
    assert wrong.json()["error"] == "invalid_code"
    # The wrong-kind attempts did not use the codes up.
    assert (await api.client.post("/api/v1/pair", json={"token": web.token})).status_code == 200
    async with api.agent_client() as c:
        ok = await c.post("/api/v1/pair", json={"token": agent.token, "client": "agent"})
    assert ok.status_code == 200


async def test_agent_short_code_pairing(api: ApiHarness) -> None:
    pairing = await api.new_pairing(kind="agent")
    async with api.agent_client() as c:
        r = await c.post(
            "/api/v1/pair",
            json={"code": pairing.short_code.lower().replace("-", " "), "client": "agent"},
        )
    assert r.status_code == 200
    assert r.json()["device_token"]


# ---------------------------------------------------------------- token transport binding


async def test_web_cookie_token_is_not_accepted_as_bearer(api: ApiHarness) -> None:
    pairing = await api.new_pairing(kind="web")
    r = await api.client.post("/api/v1/pair", json={"token": pairing.token})
    web_token = r.cookies[DEVICE_COOKIE]
    assert (await api.client.get("/api/v1/device")).status_code == 200
    async with api.agent_client(web_token) as c:
        assert (await c.get("/api/v1/device")).status_code == 401


async def test_agent_token_is_not_accepted_as_cookie(api: ApiHarness) -> None:
    token = await api.pair_agent()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.app),
        base_url=APP,
        headers={"origin": APP},
        cookies={DEVICE_COOKIE: token},
    ) as c:
        assert (await c.get("/api/v1/device")).status_code == 401


async def test_bearer_request_with_origin_header_is_refused(api: ApiHarness) -> None:
    token = await api.pair_agent()
    async with api.agent_client(token) as c:
        for origin in (APP, "https://evil.example", "null"):
            r = await c.get("/api/v1/device", headers={"origin": origin})
            assert r.status_code == 401, origin
        assert (await c.get("/api/v1/device")).status_code == 200


async def test_bearer_and_cookie_together_are_refused(api: ApiHarness) -> None:
    token = await api.pair_agent()
    async with api.agent_client(token) as c:
        r = await c.get("/api/v1/device", headers={"cookie": f"{DEVICE_COOKIE}=x"})
    assert r.status_code == 401


async def test_malformed_authorization_headers_are_refused(api: ApiHarness) -> None:
    token = await api.pair_agent()
    async with api.agent_client() as c:
        for value in (token, f"Basic {token}", "Bearer", "Bearer ", f"Bearer {token} extra"):
            r = await c.get("/api/v1/device", headers={"authorization": value})
            assert r.status_code == 401, value


async def test_cookie_posts_without_origin_are_still_blocked(api: ApiHarness) -> None:
    await api.pair()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.app), base_url=APP, cookies=api.client.cookies
    ) as c:
        r = await c.post("/api/v1/shift/end")
    assert r.status_code == 403


async def test_revoked_agent_token_stops_working(api: ApiHarness, world: Any) -> None:
    from khugjil.auth.devices import revoke_device
    from khugjil.db.tenancy import tenant_session

    token = await api.agent_ready()
    device = await _agent_device(world)
    async with tenant_session(world.db.sm, world.tenant_id) as s:
        await revoke_device(s, device["id"], by=None, now=datetime.now(UTC))
    async with api.agent_client(token) as c:
        assert (await c.get("/api/v1/device")).status_code == 401
        assert (await c.get("/api/v1/stream")).status_code == 401
    creds = [r for r in await world.rows("device_credential") if r["device_id"] == device["id"]]
    assert creds == []


# ---------------------------------------------------------------- tenant isolation


async def test_agent_never_sees_other_tenant_data(
    api: ApiHarness, world: Any, other_world: Any, mailer: Mailer
) -> None:
    token = await api.agent_ready()
    raw_b = mailer.bank_mail(FakeTxn(journal="999", description="B-ONLY-MARKER"))
    outcome_b = await other_world.ingest(raw_b)
    assert str(outcome_b.reason) == "ACCEPTED"
    await other_world.fanout(outcome_b.transaction_id)
    async with api.agent_client(token) as c:
        listing = await c.get("/api/v1/transactions")
        verify = await c.get("/api/v1/verify", params={"amount": "2800"})
        info = await c.get("/api/v1/device")
        ack = await c.post(f"/api/v1/transactions/{outcome_b.transaction_id}/ack")
        shift = await c.post(
            "/api/v1/shift/start", json={"member_id": str(other_world.member_id), "pin": PIN}
        )
    for r in (listing, verify, info):
        assert r.status_code == 200
        assert "B-ONLY-MARKER" not in r.text
        assert str(other_world.member_id) not in r.text
    assert ack.json() == {"acked": False}
    assert shift.json()["error"] == "not_allowed"
    assert "acked" not in {d["status"] for d in await other_world.rows("delivery")}


async def test_agent_token_of_one_tenant_resolves_only_that_tenant(
    api: ApiHarness, world: Any, other_world: Any
) -> None:
    token = await api.pair_agent()
    async with api.agent_client(token) as c:
        info = (await c.get("/api/v1/device")).json()
    assert info["tenant_name"] == "Тест A ХХК"
    devices_b = await other_world.app_rows("device")
    assert all(d["name"] != "Касс-API" for d in devices_b)


# ---------------------------------------------------------------- delivery + consent (§3.10)


async def test_agent_delivery_requires_consent_and_shift(
    api: ApiHarness, world: Any, mailer: Mailer
) -> None:
    from khugjil.delivery.service import fanout

    await api.agent_ready()
    device = await _agent_device(world)
    first = await world.ingest(mailer.bank_mail(FakeTxn(journal="501")))
    await fanout(api.rt.delivery_deps(), world.tenant_id, first.transaction_id)
    targets = {d["target_id"] for d in await world.rows("delivery") if d["channel"] == "sse"}
    assert device["id"] in targets

    await world.revoke_consent(world.member_id)
    second = await world.ingest(mailer.bank_mail(FakeTxn(journal="502")))
    await fanout(api.rt.delivery_deps(), world.tenant_id, second.transaction_id)
    rows = [d for d in await world.rows("delivery") if d["transaction_id"] == second.transaction_id]
    assert all(d["target_id"] != device["id"] for d in rows)


async def test_agent_devices_get_no_web_push(api: ApiHarness) -> None:
    token = await api.agent_ready()
    body = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "p" * 20, "auth": "a" * 16}}
    async with api.agent_client(token) as c:
        r = await c.post("/api/v1/push/subscribe", json=body)
    assert r.status_code == 400


# ---------------------------------------------------------------- version gate


async def test_agent_version_gate(api: ApiHarness) -> None:
    token = await api.pair_agent()
    for version in (None, "", "0.0.9", "garbage", "1.2", "01.0.0", "0.1.0-rc1"):
        async with api.agent_client(token, version=version) as c:
            r = await c.get("/api/v1/device")
        assert r.status_code == 426, version
        assert r.json()["error"] == "update_required"
    async with api.agent_client(token, version="0.1.0") as c:
        assert (await c.get("/api/v1/device")).status_code == 200
    async with api.agent_client(token, version="1.10.3") as c:
        assert (await c.get("/api/v1/device")).status_code == 200


# ---------------------------------------------------------------- feature flag off


async def test_flag_off_disables_agent_pairing_and_bearer_auth(
    api: ApiHarness, world: Any, mailer: Mailer
) -> None:
    token = await api.pair_agent()
    off = await ApiHarness.create(world, mailer.dns)
    try:
        pairing = await off.new_pairing(kind="agent")
        async with off.agent_client() as c:
            r = await c.post("/api/v1/pair", json={"token": pairing.token, "client": "agent"})
        assert r.status_code in (400, 403)
        assert "device_token" not in r.text
        async with off.agent_client(token) as c:
            assert (await c.get("/api/v1/device")).status_code == 401
    finally:
        await off.close()
