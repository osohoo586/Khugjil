"""Ingest pipeline: check a (recipient), checks b..g, then persist only the allowed result.

The raw message lives only in the arguments of ``ingest`` and of the pure ``evaluate`` call it
makes. It is never written to the database, Redis, the job queue, files or logs. The outcome is
recorded as ``ingest_event(tenant_id, reason_code, sha256(Message-ID))`` plus, when accepted, one
``transaction`` row holding SPEC §3.4 fields only.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from khugjil.crypto import Crypto, Purpose, field_aad
from khugjil.db.models import (
    Alert,
    AuditLog,
    BankAccount,
    ForwardingConfirmation,
    GroupAccount,
    IngestEvent,
    RecipientGroup,
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
    credited_account,
    evaluate,
    masked_match,
    numbers_overlap,
)
from khugjil.ingest.dkim_arc import DnsFunc
from khugjil.ingest.parsers.khan_v1 import canonical_account
from khugjil.ingest.reasons import TEMPLATE_CHANGE_REASONS, Reason
from khugjil.logs import get_logger
from khugjil.services.provisioning import account_hmac, inbound_hmac, masked_label
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
    if tenant is None:  # pragma: no cover - resolver only returns existing tenants
        return Outcome(Reason.RCPT_UNKNOWN)

    async def run_checks(message: bytes) -> Evaluation:
        lookup = _account_lookup(deps, tenant_id, await _account_rows(deps, tenant_id))
        return await asyncio.to_thread(
            evaluate,
            message,
            bank=BankConfig.from_settings(deps.settings),
            lookup_account=lookup,
            dnsfunc=deps.dnsfunc,
            include_own_transfers=tenant.include_own_transfers,
        )

    result = await run_checks(raw)
    if (
        isinstance(result, Dropped)
        and result.reason == Reason.NOT_TENANT_ACCOUNT
        and tenant.auto_add_accounts
    ):
        hint = credited_account(raw, BankConfig.from_settings(deps.settings))
        if hint is not None and await _auto_add_account(deps, tenant_id, hint.reveal()):
            result = await run_checks(raw)  # the bank-signed email named a new shop account
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


async def _account_rows(deps: IngestDeps, tenant_id: uuid.UUID) -> list[Any]:
    async with tenant_session(deps.sessionmaker, tenant_id) as s:
        return list(
            (
                await s.execute(
                    select(
                        BankAccount.id,
                        BankAccount.number_hmac,
                        BankAccount.number_enc,
                        BankAccount.status,
                    )
                )
            ).all()
        )


def _account_lookup(
    deps: IngestDeps, tenant_id: uuid.UUID, rows: list[Any]
) -> Callable[[str], AccountMatch | None]:
    accounts = {
        r.number_hmac: AccountMatch(account_id=r.id, active=r.status == "active") for r in rows
    }

    def lookup(value: str) -> AccountMatch | None:
        if "*" not in value:
            return accounts.get(deps.crypto.hmac(Purpose.ACCOUNT_HMAC, tenant_id, value))
        # Banks that mask the number (Golomt): compare against the shop's own numbers, which
        # are decrypted only here, in memory, and only for such an email.
        aad = field_aad(tenant_id, "bank_account", "number_enc")
        numbers = {
            deps.crypto.decrypt_str(r.number_enc, aad): AccountMatch(
                account_id=r.id, active=r.status == "active"
            )
            for r in rows
        }
        return masked_match(value, numbers)

    return lookup


_AUTO_NUMBER = re.compile(r"[0-9*]{6,34}")


async def _auto_add_account(deps: IngestDeps, tenant_id: uuid.UUID, number: str) -> bool:
    """Tenants with ``auto_add_accounts``: register the account a bank-signed email credited.

    Never duplicates or guesses: a number that could be any existing account (same length, same
    visible digits) is left alone. The new account joins every active group (or a new "Үндсэн"
    group) so its income reaches the shop's cashiers; the owner can rename or deactivate it.
    """
    number = canonical_account(number)
    if not _AUTO_NUMBER.fullmatch(number) or sum(c.isdigit() for c in number) < 4:
        return False
    aad = field_aad(tenant_id, "bank_account", "number_enc")
    try:
        async with tenant_session(deps.sessionmaker, tenant_id) as s:
            existing = (await s.execute(select(BankAccount.number_enc))).scalars()
            if any(numbers_overlap(deps.crypto.decrypt_str(e, aad), number) for e in existing):
                return False
            account = BankAccount(
                tenant_id=tenant_id,
                number_enc=deps.crypto.encrypt(number, aad),
                number_hmac=account_hmac(deps.crypto, tenant_id, number),
                alias=f"Данс {masked_label(number)}",
            )
            s.add(account)
            await s.flush()
            groups = list(
                await s.scalars(select(RecipientGroup.id).where(RecipientGroup.status == "active"))
            )
            if not groups:
                group = RecipientGroup(tenant_id=tenant_id, name="Үндсэн")
                s.add(group)
                await s.flush()
                groups = [group.id]
            s.add_all(
                GroupAccount(tenant_id=tenant_id, group_id=g, account_id=account.id) for g in groups
            )
            s.add(
                AuditLog(
                    tenant_id=tenant_id,
                    actor_kind="system",
                    action="account.auto_add",
                    object_type="bank_account",
                    object_id=account.id,
                    object_label=account.alias,
                )
            )
    except IntegrityError:
        return True  # added concurrently by another email: the second check finds it
    get_logger().info("ingest.account_auto_added", tenant_id=str(tenant_id))
    return True


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
