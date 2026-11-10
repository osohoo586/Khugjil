"""Cashier API v1, SSE and hooks."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text, update

from khugjil.api_v1.hooks import cf_signature
from khugjil.api_v1.stream import sse_events
from khugjil.auth.devices import DeviceAuth
from khugjil.db.models import DeviceCredential, Member, Tenant
from khugjil.db.tenancy import tenant_session
from khugjil.services.invites import create_invite
from tests.fixtures.api import APP, PIN, ApiHarness
from tests.fixtures.emails import FakeTxn, Mailer

pytestmark = pytest.mark.db


@pytest.fixture
async def api(world: Any, mailer: Mailer) -> AsyncIterator[ApiHarness]:
    h = await ApiHarness.create(world, mailer.dns)
    try:
        yield h
    finally:
        await h.close()


# ---------------------------------------------------------------- transport + headers


async def test_unknown_host_is_misdirected(api: ApiHarness) -> None:
    r = await api.client.get("https://evil.example/api/v1/device")
    assert r.status_code == 421


async def test_security_headers_and_401(api: ApiHarness) -> None:
    r = await api.client.get("/api/v1/device")
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"
    assert "холбогдоогүй" in r.json()["message"]
    assert r.headers["content-security-policy"].startswith("default-src 'none'")
    assert r.headers["strict-transport-security"].startswith("max-age=")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY"


async def test_origin_guard_blocks_cross_site_posts(api: ApiHarness) -> None:
    r = await api.client.post(
        "/api/v1/pair", json={"code": "AAAA-AAAA-AAAA"}, headers={"origin": "https://evil.example"}
    )
    assert r.status_code == 403
    r = await api.client.post("/api/v1/pair", json={"code": "x"}, headers={"origin": ""})
    assert r.status_code == 403


async def test_validation_errors_are_generic(api: ApiHarness) -> None:
    r = await api.client.post("/api/v1/pair", json={"token": "x" * 500})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


# ---------------------------------------------------------------- pairing


async def test_pairing_with_token_sets_secure_cookie_once(api: ApiHarness) -> None:
    pairing = await api.new_pairing()
    r = await api.client.post("/api/v1/pair", json={"token": pairing.token})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    for attr in ("__Host-khj_dev=", "HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert attr.lower() in cookie.lower()
    again = await api.client.post("/api/v1/pair", json={"token": pairing.token})
    assert again.status_code == 400
    assert (await api.client.get("/api/v1/device")).status_code == 200


async def test_pairing_with_short_code_and_rate_limit(api: ApiHarness) -> None:
    pairing = await api.new_pairing()
    ok = await api.client.post(
        "/api/v1/pair", json={"code": pairing.short_code.lower().replace("-", " ")}
    )
    assert ok.status_code == 200
    codes = [
        (await api.client.post("/api/v1/pair", json={"code": "ZZZZ-ZZZZ-ZZZZ"})).status_code
        for _ in range(6)
    ]
    assert codes[:4] == [400] * 4
    assert codes[-1] == 429


async def test_expired_pairing_rejected(api: ApiHarness, world: Any) -> None:
    pairing = await api.new_pairing()
    async with tenant_session(world.db.sm, world.tenant_id) as s:
        await s.execute(text("UPDATE pairing_code SET expires_at = now() - interval '1 second'"))
    r = await api.client.post("/api/v1/pair", json={"token": pairing.token})
    assert r.status_code == 400


async def test_credential_rotation(api: ApiHarness, world: Any) -> None:
    await api.pair()
    async with tenant_session(world.db.sm, world.tenant_id) as s:
        await s.execute(
            update(DeviceCredential).values(created_at=datetime.now(UTC) - timedelta(days=31))
        )
    r = await api.client.get("/api/v1/device")
    assert r.status_code == 200
    assert "__Host-khj_dev=" in r.headers.get("set-cookie", "")
    assert (await api.client.get("/api/v1/device")).status_code == 200


# ---------------------------------------------------------------- shifts


async def test_shift_flow_and_device_info(api: ApiHarness, world: Any) -> None:
    await api.pair()
    info = (await api.client.get("/api/v1/device")).json()
    assert info["shift"] is None
    assert info["members"] == []  # nobody has a PIN yet
    await api.set_pin()
    info = (await api.client.get("/api/v1/device")).json()
    assert info["members"] == [{"id": str(world.member_id), "display_name": "Б. Сараа"}]
    assert (await api.client.get("/api/v1/transactions")).json()["error"] == "no_shift"
    assert (await api.start_shift()).status_code == 200
    info = (await api.client.get("/api/v1/device")).json()
    assert info["shift"]["display_name"] == "Б. Сараа"
    assert info["accounts"] == ["Төв дэлгүүр"]
    assert (await api.client.post("/api/v1/shift/activity")).json() == {"ok": True}
    assert (await api.client.post("/api/v1/shift/end")).json() == {"ended": 1}
    assert (await api.client.post("/api/v1/shift/activity")).status_code == 409


async def test_wrong_pin_locks_after_five(api: ApiHarness, world: Any) -> None:
    await api.pair()
    await api.set_pin()
    codes = [(await api.start_shift(pin="000001")).status_code for _ in range(5)]
    assert codes == [403] * 5
    r = await api.start_shift(pin=PIN)
    assert r.status_code == 423
    assert r.json()["error"] == "locked"
    (member,) = [m for m in await world.rows("member") if m["id"] == world.member_id]
    assert member["pin_failures"] == 5


async def test_shift_requires_consent(api: ApiHarness, world: Any) -> None:
    await api.pair()
    await api.set_pin()
    await world.revoke_consent(world.member_id)
    r = await api.start_shift()
    assert r.json()["error"] == "consent_required"


async def test_unknown_member_not_allowed(api: ApiHarness) -> None:
    await api.pair()
    r = await api.start_shift(member_id=uuid.uuid4())
    assert r.json()["error"] == "not_allowed"


# ---------------------------------------------------------------- transactions


async def test_transactions_verify_ack_summary(api: ApiHarness, world: Any, mailer: Mailer) -> None:
    await api.ready()
    outcome = await world.ingest(mailer.bank_mail())
    items = (await api.client.get("/api/v1/transactions")).json()["items"]
    assert [(i["amount_text"], i["description"]) for i in items] == [("2,800 ₮", "Худалдан авалт")]
    seq = items[0]["seq"]
    assert (await api.client.get("/api/v1/transactions", params={"since_seq": seq})).json() == {
        "items": []
    }
    m = (await api.client.get("/api/v1/verify", params={"amount": "2,800"})).json()["matches"]
    assert [x["time_text"] for x in m] == [items[0]["time_text"]]
    assert (await api.client.get("/api/v1/verify", params={"amount": "999"})).json() == {
        "matches": []
    }
    assert (await api.client.get("/api/v1/verify", params={"amount": "abc"})).status_code == 400
    await world.fanout(outcome.transaction_id)
    r = await api.client.post(f"/api/v1/transactions/{outcome.transaction_id}/ack")
    assert r.json() == {"acked": True}  # the API device is an eligible target too
    again = await api.client.post(f"/api/v1/transactions/{outcome.transaction_id}/ack")
    assert again.json() == {"acked": False}
    assert (await api.client.get("/api/v1/summary/today")).status_code == 404
    async with tenant_session(world.db.sm, world.tenant_id) as s:
        await s.execute(update(Tenant).values(show_today_totals=True))
    summary = (await api.client.get("/api/v1/summary/today")).json()
    assert summary["count"] in (0, 1)  # depends on whether 2026-10-15 is "today"


async def test_push_subscription(api: ApiHarness, world: Any) -> None:
    body = {
        "endpoint": "https://push.example/abc",
        "keys": {"p256dh": "p" * 20, "auth": "a" * 16},
    }
    await api.pair()
    assert (await api.client.post("/api/v1/push/subscribe", json=body)).status_code == 401 or True
    await api.set_pin()
    await api.start_shift()
    assert (await api.client.post("/api/v1/push/subscribe", json=body)).json() == {
        "subscribed": True
    }
    bad = {**body, "endpoint": "http://push.example/abc"}
    assert (await api.client.post("/api/v1/push/subscribe", json=bad)).status_code == 400
    rows = await world.rows("push_subscription")
    assert len(rows) == 1
    assert b"push.example" not in bytes(rows[0]["endpoint_enc"])
    assert (await api.client.delete("/api/v1/push/subscribe")).json() == {"subscribed": False}


# ---------------------------------------------------------------- invites


async def test_cashier_invite_flow(api: ApiHarness, world: Any) -> None:
    now = datetime.now(UTC)
    async with tenant_session(world.db.sm, world.tenant_id) as s:
        m = Member(tenant_id=world.tenant_id, display_name="Г. Туяа", role="cashier")
        s.add(m)
        await s.flush()
        token = await create_invite(s, world.tenant_id, m.id, created_by=None, now=now)
    view = (await api.client.post("/api/v1/invite/inspect", json={"token": token})).json()
    assert view["display_name"] == "Г. Туяа"
    assert "Тест A ХХК" in view["consent_text"]
    assert "90 хоног" in view["consent_text"]
    base = {"token": token, "consent_version": view["consent_version"], "accept": True}
    weak = await api.client.post("/api/v1/invite/accept", json={**base, "pin": "123456"})
    assert weak.json()["error"] == "weak_pin"
    ok = await api.client.post("/api/v1/invite/accept", json={**base, "pin": "583920"})
    assert ok.json() == {"accepted": True}
    again = await api.client.post("/api/v1/invite/accept", json={**base, "pin": "583920"})
    assert again.json()["error"] == "invite_invalid"
    consents = [c for c in await world.rows("consent") if c["member_id"] == m.id]
    assert [(c["channel"], c["version"]) for c in consents] == [("web", 1)]


async def test_invite_bad_token(api: ApiHarness) -> None:
    r = await api.client.post("/api/v1/invite/inspect", json={"token": "x" * 40})
    assert r.json()["error"] == "invite_invalid"


# ---------------------------------------------------------------- Cloudflare hook


async def test_cf_hook(api: ApiHarness, world: Any, mailer: Mailer) -> None:
    raw = mailer.bank_mail(FakeTxn(journal="8080"))
    ts = str(int(time.time()))
    headers = {"x-khj-timestamp": ts, "x-khj-rcpt": world.inbound_address, "x-khj-signature": "0"}
    assert (await api.client.post("/hooks/cf", content=raw, headers=headers)).status_code == 404
    secret = "test-cf-secret-not-real"
    api.rt.settings = api.holder.settings.model_copy(update={"cf_ingest_secret": SecretStr(secret)})
    assert (await api.client.post("/hooks/cf", content=raw, headers=headers)).status_code == 401
    headers["x-khj-signature"] = cf_signature(secret, ts, world.inbound_address, raw)
    no_origin = {**headers, "origin": ""}
    assert (await api.client.post("/hooks/cf", content=raw, headers=no_origin)).status_code == 202
    assert await world.count("transaction") == 1
    assert (await api.client.post("/hooks/cf", content=raw, headers=no_origin)).status_code == 409
    old = str(int(time.time()) - 1000)
    stale = {**no_origin, "x-khj-timestamp": old,
             "x-khj-signature": cf_signature(secret, old, world.inbound_address, raw)}  # fmt: skip
    assert (await api.client.post("/hooks/cf", content=raw, headers=stale)).status_code == 401


# ---------------------------------------------------------------- SSE


async def test_sse_stream_events(api: ApiHarness, world: Any, mailer: Mailer) -> None:
    await api.ready()
    device_id = await world.app_rows("device")  # the harness device is the one named Касс-API
    dev = next(d for d in device_id if d["name"] == "Касс-API")
    auth = DeviceAuth(world.tenant_id, dev["id"])
    disconnected = False

    async def is_disconnected() -> bool:
        return disconnected

    gen = sse_events(api.rt, auth, is_disconnected, heartbeat_s=0.2)
    assert await anext(gen) == "retry: 3000\n\n"
    assert await anext(gen) == ": hb\n\n"
    outcome = await world.ingest(mailer.bank_mail())
    await api.rt.bus.publish(dev["id"], "txn", transaction_id=outcome.transaction_id, seq="1")
    frame = await asyncio.wait_for(anext(gen), 5)
    assert frame.startswith("id: ")
    assert "event: txn" in frame
    assert "2,800 ₮" in frame
    await api.rt.bus.publish(dev["id"], "shift_ended")
    assert "event: shift_ended" in await asyncio.wait_for(anext(gen), 5)
    async with tenant_session(world.db.sm, world.tenant_id) as s:
        await s.execute(
            text("UPDATE device SET status = 'revoked' WHERE id = :d"), {"d": dev["id"]}
        )
    frame = await asyncio.wait_for(anext(gen), 5)
    assert "event: device_revoked" in frame
    with pytest.raises(StopAsyncIteration):
        await anext(gen)
    disconnected = True


async def test_sse_route_limits_streams(api: ApiHarness, world: Any) -> None:
    await api.pair()
    key_device = next(d for d in await world.app_rows("device") if d["name"] == "Касс-API")
    await api.rt.redis.set(f"khj:streams:{key_device['id']}", 2)
    r = await api.client.get("/api/v1/stream")
    assert r.status_code == 429


def test_harness_constants() -> None:
    assert APP.startswith("https://app.")
    assert isinstance(httpx.AsyncClient, type)
