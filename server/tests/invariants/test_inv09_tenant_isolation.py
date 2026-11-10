"""SPEC §3.9: strict tenant isolation, proven by tests.

Route-level isolation (every API/admin endpoint probed with another tenant's ids) is added in
the API and admin-panel steps, in tests/invariants/test_inv09_routes.py.
"""

from __future__ import annotations

import pytest

from tests.fixtures.emails import TENANT_ACCOUNT, FakeTxn, Mailer
from tests.invariants.helpers import pending

pytestmark = [pytest.mark.invariant(9), pending("database + RLS"), pytest.mark.db]


async def test_every_tenant_table_has_forced_rls(world) -> None:  # type: ignore[no-untyped-def]
    tables = await world.db.tenant_tables()
    assert len(tables) >= 20
    for t in tables:
        assert t.rls_enabled, t.name
        assert t.rls_forced, t.name
        assert t.policy_count >= 1, t.name


async def test_app_role_cannot_bypass_rls(world) -> None:  # type: ignore[no-untyped-def]
    role = await world.db.app_role_attributes()
    assert role == {"superuser": False, "bypassrls": False}


async def test_tenant_context_sees_only_own_rows(world, other_world, mailer: Mailer) -> None:  # type: ignore[no-untyped-def]
    assert str((await world.ingest(mailer.bank_mail())).reason) == "ACCEPTED"
    raw_b = mailer.bank_mail(FakeTxn(journal="555"), message_id="<b@bank.example>")
    assert str((await other_world.ingest(raw_b)).reason) == "ACCEPTED"
    for table in ("transaction", "bank_account", "member", "device", "recipient_group"):
        mine = await world.app_rows(table)
        theirs = await other_world.app_rows(table)
        assert mine
        assert theirs
        assert {r["tenant_id"] for r in mine} == {world.tenant_id}
        assert {r["tenant_id"] for r in theirs} == {other_world.tenant_id}


async def test_no_tenant_context_sees_nothing(world) -> None:  # type: ignore[no-untyped-def]
    assert await world.db.app_rows_without_tenant("bank_account") == []


async def test_composite_fk_blocks_cross_tenant_links(world, other_world) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(Exception, match="foreign key"):
        await world.db.link_group_account_raw(
            tenant_id=world.tenant_id, group_id=world.group_id, account_id=other_world.account_id
        )


async def test_other_tenants_account_email_is_dropped(world, other_world, mailer: Mailer) -> None:  # type: ignore[no-untyped-def]
    raw = mailer.bank_mail(FakeTxn(to_account="4444444444"))
    await other_world.add_account("4444444444")
    assert str((await world.ingest(raw)).reason) == "NOT_TENANT_ACCOUNT"


async def test_account_hmac_differs_per_tenant(world, other_world) -> None:  # type: ignore[no-untyped-def]
    await other_world.add_account(TENANT_ACCOUNT)
    rows = await world.rows("bank_account")
    hmacs = [r["number_hmac"] for r in rows if r["status"] == "active"]
    assert len(hmacs) == len(set(hmacs))
