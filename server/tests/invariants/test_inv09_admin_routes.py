"""SPEC §3.9 for the admin panel: an owner of tenant A gets 404 for every object of tenant B."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from tests.fixtures.api import ADMIN, ApiHarness, admin_login, make_admin
from tests.fixtures.emails import FakeTxn, Mailer

pytestmark = [pytest.mark.invariant(9), pytest.mark.db]


@pytest.fixture
async def owner_a(world: Any, mailer: Mailer) -> AsyncIterator[tuple[ApiHarness, str]]:
    h = await ApiHarness.create(world, mailer.dns, base_url=ADMIN)
    _, login, secret = await make_admin(world, "owner")
    csrf = await admin_login(h, login, secret)
    try:
        yield h, csrf
    finally:
        await h.close()


async def test_other_tenant_object_ids_are_404(
    owner_a: tuple[ApiHarness, str], other_world: Any
) -> None:
    h, csrf = owner_a
    b = other_world
    gets = [f"/groups/{b.group_id}", f"/members/{b.member_id}", f"/devices/{b.device_id}"]
    for path in gets:
        assert (await h.client.get(path)).status_code == 404, path
    posts = {
        f"/accounts/{b.account_id}": {"alias": "x", "status": "inactive"},
        f"/groups/{b.group_id}": {"name": "x", "status": "active"},
        f"/members/{b.member_id}": {"display_name": "x", "role": "cashier"},
        f"/members/{b.member_id}/revoke": {},
        f"/members/{b.member_id}/reinvite": {},
        f"/members/{b.member_id}/consent/revoke": {},
        f"/members/{b.member_id}/schedule": {"weekday": 0, "start": "09:00", "end": "10:00"},
        f"/devices/{b.device_id}": {"name": "x"},
        f"/devices/{b.device_id}/revoke": {},
    }
    for path, data in posts.items():
        r = await h.client.post(path, data={"csrf": csrf, **data})
        assert r.status_code == 404, (path, r.status_code)
    rows = await b.rows("member")
    assert all(m["status"] == "active" for m in rows if m["tenant_id"] == b.tenant_id)


async def test_cannot_link_other_tenant_objects_into_own_group(
    owner_a: tuple[ApiHarness, str], world: Any, other_world: Any
) -> None:
    h, csrf = owner_a
    for field, value in (
        ("account_ids", other_world.account_id),
        ("member_ids", other_world.member_id),
        ("device_ids", other_world.device_id),
    ):
        data = {"csrf": csrf, "name": "A", "status": "active", field: [str(value)]}
        assert (await h.client.post(f"/groups/{world.group_id}", data=data)).status_code == 404
    pair = {"csrf": csrf, "device_name": "X", "group_ids": [str(other_world.group_id)]}
    assert (await h.client.post("/devices/pair", data=pair)).status_code == 400


async def test_history_and_email_show_only_own_data(
    owner_a: tuple[ApiHarness, str], other_world: Any, mailer: Mailer
) -> None:
    h, _ = owner_a
    await other_world.ingest(mailer.bank_mail(FakeTxn(journal="77", description="B-TENANT-ONLY")))
    for path in (
        "/history",
        f"/history?account={other_world.account_id}",
        "/history.csv",
        "/email",
        "/",
    ):
        r = await h.client.get(path)
        assert "B-TENANT-ONLY" not in r.text
        assert other_world.inbound_address not in r.text
