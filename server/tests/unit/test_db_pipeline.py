"""Database-backed tests for the schema, tenancy helpers and the ingest pipeline."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text, update

from khugjil.crypto import field_aad, token_hash
from khugjil.db.models import Base, Tenant
from khugjil.db.tenancy import Resolver, tenant_session
from khugjil.ingest.reasons import Reason
from khugjil.services.provisioning import current_inbound, rotate_inbound
from tests.fixtures.emails import FakeTxn, Mailer, forwarding_confirmation

pytestmark = pytest.mark.db


async def test_models_match_migrated_schema(world: Any) -> None:
    def diff(conn: Any) -> list[Any]:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        return list(compare_metadata(ctx, Base.metadata))

    def is_known(change: Any) -> bool:
        # transaction -> bank_account uses ON DELETE SET NULL (bank_account_id) in the migration
        # (PostgreSQL column-list syntax SQLAlchemy cannot express); alembic reports it as a swap.
        kind, fk = change[0], change[1]
        return (
            kind in {"add_fk", "remove_fk"}
            and fk.table.name == "transaction"
            and [c.name for c in fk.columns] == ["tenant_id", "bank_account_id"]
        )

    async with world.db.su.connect() as c:
        changes = await c.run_sync(diff)
    assert [c for c in changes if not is_known(c)] == []


async def test_consent_documents_seeded_and_read_only(world: Any) -> None:
    rows = await world.rows("consent_document")
    assert {(r["kind"], r["version"]) for r in rows} == {("member", 1), ("tenant_terms", 1)}
    with pytest.raises(Exception, match="permission denied"):
        async with world.db.sm() as s, s.begin():
            await s.execute(text("UPDATE consent_document SET body = 'x'"))


async def test_audit_log_is_append_only_for_app_role(world: Any) -> None:
    async with tenant_session(world.db.sm, world.tenant_id) as s:
        await s.execute(
            text(
                "INSERT INTO audit_log (tenant_id, actor_kind, action) VALUES (:t, 'system', 'x')"
            ),
            {"t": world.tenant_id},
        )
    with pytest.raises(Exception, match="permission denied"):
        async with tenant_session(world.db.sm, world.tenant_id) as s:
            await s.execute(text("DELETE FROM audit_log"))


async def test_tenant_session_requires_uuid(world: Any) -> None:
    with pytest.raises(TypeError):
        async with tenant_session(world.db.sm, str(world.tenant_id)):  # type: ignore[arg-type]
            pass


async def test_resolver_unknown_values_return_none(world: Any) -> None:
    r = Resolver(world.db.sm)
    h = token_hash("nope")
    assert await r.inbound(h) is None
    assert await r.device_token(h) is None
    assert await r.session_token(h) is None
    assert await r.login("nobody") is None
    assert await r.pairing_token(h) is None
    assert await r.pairing_short(h) is None
    assert await r.invite(h) is None
    assert await r.telegram_chat(h) is None
    assert await r.telegram_code(h) is None
    assert await r.active_tenants() == [world.tenant_id]


async def test_wrong_inbound_domain_is_unknown(world: Any, mailer: Mailer) -> None:
    local = world.inbound_address.split("@")[0]
    for rcpt in (f"{local}@attacker.example", "no-at-sign", f"<{world.inbound_address}>"):
        outcome = await world.ingest(mailer.bank_mail(), rcpt=rcpt)
        expected = Reason.ACCEPTED if rcpt.startswith("<") else Reason.RCPT_UNKNOWN
        assert outcome.reason == expected


async def test_rotated_address_stops_immediately(world: Any, mailer: Mailer) -> None:
    async with tenant_session(world.db.sm, world.tenant_id) as s:
        new_local = await rotate_inbound(s, world.db.crypto, world.tenant_id)
        assert await current_inbound(s, world.db.crypto, world.tenant_id) == new_local
    assert (await world.ingest(mailer.bank_mail())).reason == Reason.RCPT_UNKNOWN
    new_rcpt = f"{new_local}@{world.db.settings.inbound_domain}"
    assert (await world.ingest(mailer.bank_mail(), rcpt=new_rcpt)).reason == Reason.ACCEPTED


async def test_suspended_tenant_rejects_mail(world: Any, mailer: Mailer) -> None:
    async with tenant_session(world.db.sm, world.tenant_id) as s:
        await s.execute(update(Tenant).values(status="suspended"))
    assert (await world.ingest(mailer.bank_mail())).reason == Reason.RCPT_UNKNOWN


async def test_accepted_transaction_fields(world: Any, mailer: Mailer) -> None:
    outcome = await world.ingest(mailer.bank_mail())
    assert outcome.reason == Reason.ACCEPTED
    assert world.enqueued == [(world.tenant_id, outcome.transaction_id)]
    (row,) = await world.rows("transaction")
    assert row["bank_account_id"] == world.account_id
    assert str(row["amount"]) == "2800.00"
    assert row["parser_version"] == "khan_v1"
    desc = world.db.crypto.decrypt_str(
        row["description_enc"], field_aad(world.tenant_id, "transaction", "description_enc")
    )
    assert desc == "Худалдан авалт"
    (event,) = await world.rows("ingest_event")
    assert event["reason_code"] == "ACCEPTED"


async def test_forwarding_code_stored_encrypted_with_expiry(world: Any, mailer: Mailer) -> None:
    raw = mailer.dkim_sign(
        forwarding_confirmation("555666777"), domain="google.com", selector="g1", key="google"
    )
    outcome = await world.ingest(raw)
    assert outcome.reason == Reason.FWD_CONFIRM_STORED
    (row,) = await world.rows("forwarding_confirmation")
    assert b"555666777" not in bytes(row["code_enc"])
    assert row["expires_at"] - row["received_at"] == timedelta(hours=24)
    code = world.db.crypto.decrypt_str(
        row["code_enc"], field_aad(world.tenant_id, "forwarding_confirmation", "code_enc")
    )
    assert code == "555666777"


async def test_template_change_alert_opens_once_and_resolves(world: Any, mailer: Mailer) -> None:
    for i in range(3):
        raw = mailer.bank_mail(subject="Changed subject", message_id=f"<c{i}@bank.example>")
        assert (await world.ingest(raw)).reason == Reason.SUBJECT_MISMATCH
    alerts = await world.rows("alert")
    assert [(a["kind"], a["resolved_at"]) for a in alerts] == [("template_changed", None)]
    assert (await world.ingest(mailer.bank_mail())).reason == Reason.ACCEPTED
    (alert,) = await world.rows("alert")
    assert alert["resolved_at"] is not None


async def test_auth_failures_do_not_open_template_alert(world: Any, mailer: Mailer) -> None:
    from tests.fixtures.emails import build_message

    await world.ingest(build_message(subject="Changed subject"))
    assert await world.rows("alert") == []


async def test_inactive_account_is_dropped(world: Any, mailer: Mailer) -> None:
    async with tenant_session(world.db.sm, world.tenant_id) as s:
        await s.execute(text("UPDATE bank_account SET status = 'inactive'"))
    outcome = await world.ingest(mailer.bank_mail(FakeTxn(journal=str(uuid.uuid4().int)[:9])))
    assert outcome.reason == Reason.ACCOUNT_INACTIVE
