"""Ingest pipeline: check a (recipient), checks b..g, then persist only the allowed result.

The raw message lives only in the arguments of ``ingest`` and of the pure ``evaluate`` call it
makes. It is never written to the database, Redis, the job queue, files or logs. The outcome is
recorded as ``ingest_event(tenant_id, reason_code, sha256(Message-ID))`` plus, when accepted, one
``transaction`` row holding SPEC §3.4 fields only.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from khugjil.crypto import Crypto, Purpose, field_aad
from khugjil.db.models import (
    Alert,
    BankAccount,
    ForwardingConfirmation,
    IngestEvent,
    Tenant,
    Transaction,
)
from khugjil.db.tenancy import Resolver, SessionMaker, tenant_session
from khugjil.ingest.checks import (
    Accepted,
    AccountMatch,
    BankConfig,
    Dropped,
    Evaluation,
    ForwardingCode,
    evaluate,
)
from khugjil.ingest.dkim_arc import DnsFunc
from khugjil.ingest.reasons import TEMPLATE_CHANGE_REASONS, Reason
from khugjil.logs import get_logger
from khugjil.services.provisioning import inbound_hmac
from khugjil.settings import Settings

FORWARDING_CODE_TTL = timedelta(hours=24)
ALERT_TEMPLATE_CHANGED = "template_changed"
ALERT_FORWARDING_SILENT = "forwarding_silent"

EnqueueFanout = Callable[[uuid.UUID, uuid.UUID], Awaitable[None]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class IngestDeps:
    settings: Settings
    crypto: Crypto
    sessionmaker: SessionMaker
    dnsfunc: DnsFunc
    enqueue_fanout: EnqueueFanout
    now: Callable[[], datetime] = field(default=_utcnow)

    @property
    def resolver(self) -> Resolver:
        return Resolver(self.sessionmaker)


@dataclass(frozen=True)
class Outcome:
    reason: Reason
    tenant_id: uuid.UUID | None = None
    transaction_id: uuid.UUID | None = None


def idempotency_key(
    crypto: Crypto, tenant_id: uuid.UUID, journal_no: str, system_date: str, to_account: str
) -> bytes:
    return crypto.hmac(Purpose.IDEMPOTENCY, tenant_id, journal_no, system_date, to_account)


async def resolve_recipient(deps: IngestDeps, rcpt: str) -> uuid.UUID | None:
    """Check a: the envelope recipient must be an active inbound address of an active tenant."""
    _, address = parseaddr(rcpt)
    local, sep, domain = address.rpartition("@")
    if not sep or domain.lower() != deps.settings.inbound_domain.lower() or not local:
        return None
    return await deps.resolver.inbound(inbound_hmac(deps.crypto, local))


async def ingest(deps: IngestDeps, rcpt: str, raw: bytes, transport: str = "smtp") -> Outcome:
    log = get_logger()
    tenant_id = await resolve_recipient(deps, rcpt)
    if tenant_id is None:
        log.info("ingest.rejected", reason_code=Reason.RCPT_UNKNOWN.value, transport=transport)
        return Outcome(Reason.RCPT_UNKNOWN)

    async with tenant_session(deps.sessionmaker, tenant_id) as s:
        tenant = await s.get(Tenant, tenant_id)
        rows = (
            await s.execute(select(BankAccount.id, BankAccount.number_hmac, BankAccount.status))
        ).all()
    if tenant is None:  # pragma: no cover - resolver only returns existing tenants
        return Outcome(Reason.RCPT_UNKNOWN)
    accounts = {
        r.number_hmac: AccountMatch(account_id=r.id, active=r.status == "active") for r in rows
    }

    def lookup(canonical: str) -> AccountMatch | None:
        return accounts.get(deps.crypto.hmac(Purpose.ACCOUNT_HMAC, tenant_id, canonical))

    result: Evaluation = await asyncio.to_thread(
        evaluate,
        raw,
        bank=BankConfig.from_settings(deps.settings),
        lookup_account=lookup,
        dnsfunc=deps.dnsfunc,
        include_own_transfers=tenant.include_own_transfers,
    )
    del raw  # nothing below may touch the message

    outcome = await _persist(deps, tenant_id, result)
    log.info(
        "ingest.result",
        tenant_id=str(tenant_id),
        reason_code=outcome.reason.value,
        msgid_sha256=result.msgid_sha256,
        transport=transport,
        transaction_id=str(outcome.transaction_id) if outcome.transaction_id else None,
    )
    if outcome.transaction_id is not None:
        await deps.enqueue_fanout(tenant_id, outcome.transaction_id)
    return outcome


async def _persist(deps: IngestDeps, tenant_id: uuid.UUID, result: Evaluation) -> Outcome:
    now = deps.now()
    async with tenant_session(deps.sessionmaker, tenant_id) as s:
        if isinstance(result, ForwardingCode):
            s.add(
                ForwardingConfirmation(
                    tenant_id=tenant_id,
                    code_enc=deps.crypto.encrypt(
                        result.code.reveal(),
                        field_aad(tenant_id, "forwarding_confirmation", "code_enc"),
                    ),
                    received_at=now,
                    expires_at=now + FORWARDING_CODE_TTL,
                )
            )
            reason, txn_id = Reason.FWD_CONFIRM_STORED, None
        elif isinstance(result, Dropped):
            reason, txn_id = result.reason, None
            if reason in TEMPLATE_CHANGE_REASONS:
                await s.execute(
                    insert(Alert)
                    .values(tenant_id=tenant_id, kind=ALERT_TEMPLATE_CHANGED)
                    .on_conflict_do_nothing(
                        index_elements=["tenant_id", "kind"],
                        index_where=Alert.resolved_at.is_(None),
                    )
                )
        else:
            txn_id = await _insert_transaction(deps, s, tenant_id, result, now)
            reason = Reason.ACCEPTED if txn_id else Reason.DUPLICATE
            if txn_id:
                await s.execute(
                    update(Alert)
                    .where(
                        Alert.kind.in_([ALERT_TEMPLATE_CHANGED, ALERT_FORWARDING_SILENT]),
                        Alert.resolved_at.is_(None),
                    )
                    .values(resolved_at=now)
                )
        s.add(
            IngestEvent(
                tenant_id=tenant_id,
                reason_code=reason.value,
                msgid_sha256=result.msgid_sha256,
                received_at=now,
            )
        )
    return Outcome(reason, tenant_id, txn_id)


async def _insert_transaction(
    deps: IngestDeps, s: AsyncSession, tenant_id: uuid.UUID, a: Accepted, now: datetime
) -> uuid.UUID | None:
    to_account = a.to_account.reveal()
    stmt = (
        insert(Transaction)
        .values(
            tenant_id=tenant_id,
            bank_account_id=a.account_id,
            to_account_hmac=deps.crypto.hmac(Purpose.ACCOUNT_HMAC, tenant_id, to_account),
            journal_hmac=deps.crypto.hmac(Purpose.JOURNAL_HMAC, tenant_id, a.journal_no),
            idempotency_key=idempotency_key(
                deps.crypto, tenant_id, a.journal_no, a.system_date, to_account
            ),
            amount=a.amount,
            currency=a.currency,
            txn_at=a.txn_at,
            description_enc=deps.crypto.encrypt(
                a.description, field_aad(tenant_id, "transaction", "description_enc")
            ),
            parser_version=a.parser_version,
            received_at=now,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(Transaction.id)
    )
    return (await s.execute(stmt)).scalar_one_or_none()
