"""SPEC §3.9 at the HTTP layer: a device of tenant A can never see or touch tenant B's data.

Admin-panel routes are covered in test_inv09_admin_routes.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from tests.fixtures.api import ApiHarness
from tests.fixtures.emails import FakeTxn, Mailer

pytestmark = [pytest.mark.invariant(9), pytest.mark.db]


@pytest.fixture
async def api(world: Any, mailer: Mailer) -> AsyncIterator[ApiHarness]:
    h = await ApiHarness.create(world, mailer.dns)
    try:
        await h.ready()
        yield h
    finally:
        await h.close()


async def test_device_never_sees_other_tenant_transactions(
    api: ApiHarness, world: Any, other_world: Any, mailer: Mailer
) -> None:
    raw_b = mailer.bank_mail(FakeTxn(journal="999", description="B-ONLY-MARKER"))
    assert str((await other_world.ingest(raw_b)).reason) == "ACCEPTED"
    listing = await api.client.get("/api/v1/transactions")
    verify = await api.client.get("/api/v1/verify", params={"amount": "2800"})
    for r in (listing, verify):
        assert r.status_code == 200
        assert "B-ONLY-MARKER" not in r.text


async def test_cannot_ack_other_tenant_transaction(
    api: ApiHarness, other_world: Any, mailer: Mailer
) -> None:
    outcome = await other_world.ingest(mailer.bank_mail(FakeTxn(journal="998")))
    await other_world.fanout(outcome.transaction_id)
    r = await api.client.post(f"/api/v1/transactions/{outcome.transaction_id}/ack")
    assert r.json() == {"acked": False}
    statuses = {d["status"] for d in await other_world.rows("delivery")}
    assert "acked" not in statuses


async def test_cannot_start_shift_as_other_tenant_member(api: ApiHarness, other_world: Any) -> None:
    await api.set_pin(other_world.member_id)
    r = await api.start_shift(member_id=other_world.member_id)
    assert r.json()["error"] == "not_allowed"


async def test_device_info_lists_only_own_members(api: ApiHarness, other_world: Any) -> None:
    info = (await api.client.get("/api/v1/device")).json()
    assert str(other_world.member_id) not in str(info)
    assert other_world.tenant_id not in {info.get("tenant_id")}


async def test_pairing_code_of_other_tenant_pairs_into_that_tenant_only(
    api: ApiHarness, world: Any, other_world: Any, mailer: Mailer
) -> None:
    other = await ApiHarness.create(other_world, mailer.dns)
    try:
        await other.pair()
        info = (await other.client.get("/api/v1/device")).json()
        assert info["tenant_name"] == "Тест B ХХК"
    finally:
        await other.close()
