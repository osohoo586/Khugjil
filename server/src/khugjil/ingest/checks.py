"""Checks b..g (SPEC §3.2) as one pure function.

``evaluate`` needs no database: the caller passes ``lookup_account`` (canonical account number ->
AccountMatch) for the tenant identified by check a. The result is ``Dropped`` (reason + msgid
hash, nothing else), ``Accepted`` (only the fields SPEC §3.4 allows to persist) or
``ForwardingCode``. Raw account numbers stay wrapped in ``Sensitive`` and the payer account never
leaves this function.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from khugjil.crypto import sha256_hex
from khugjil.ingest import mime
from khugjil.ingest.denylist import contains_denied
from khugjil.ingest.dkim_arc import DnsFunc, authenticate
from khugjil.ingest.forwarding import confirmation_code, sender_address
from khugjil.ingest.parsers import khan_v1
from khugjil.ingest.reasons import Reason
from khugjil.ingest.sanitize import clean_text, sanitize_description
from khugjil.sensitive import Sensitive
from khugjil.settings import Settings

SINGLETON_HEADERS = ("From", "Subject", "Date", "To")


@dataclass(frozen=True)
class BankConfig:
    senders: frozenset[str]
    dkim_domains: frozenset[str]
    subject_re: re.Pattern[str]

    @classmethod
    def from_settings(cls, settings: Settings) -> BankConfig:
        return cls(
            senders=frozenset(settings.bank_senders),
            dkim_domains=frozenset(settings.bank_dkim_domains),
            subject_re=settings.subject_pattern,
        )


@dataclass(frozen=True)
class AccountMatch:
    account_id: UUID
    active: bool


AccountLookup = Callable[[str], AccountMatch | None]


@dataclass(frozen=True)
class Dropped:
    reason: Reason
    msgid_sha256: str


@dataclass(frozen=True)
class ForwardingCode:
    code: Sensitive
    msgid_sha256: str


@dataclass(frozen=True)
class Accepted:
    msgid_sha256: str
    account_id: UUID
    to_account: Sensitive
    journal_no: str
    system_date: str
    amount: Decimal
    currency: str
    txn_at: datetime
    description: str
    parser_version: str


Evaluation = Accepted | Dropped | ForwardingCode


def evaluate(
    raw: bytes,
    *,
    bank: BankConfig,
    lookup_account: AccountLookup,
    dnsfunc: DnsFunc,
    include_own_transfers: bool = False,
) -> Evaluation:
    try:
        msg = mime.parse_message(raw)
    except Exception:
        return Dropped(Reason.MIME_INVALID, sha256_hex(""))
    msgid = mime.message_id_hash(msg)

    def drop(reason: Reason) -> Dropped:
        return Dropped(reason, msgid)

    code = confirmation_code(msg, raw, dnsfunc)
    if code is not None:
        return ForwardingCode(Sensitive(code), msgid)

    # b: exactly one of each singleton header; the sender is on the allowlist.
    for name in SINGLETON_HEADERS:
        if len(msg.get_all(name) or []) > 1:
            return drop(Reason.HEADER_DUPLICATE)
    if sender_address(msg) not in bank.senders:
        return drop(Reason.FROM_NOT_ALLOWED)

    # c: DKIM for the bank domain, or Gmail ARC recording a bank DKIM pass.
    if not authenticate(raw, bank.dkim_domains, dnsfunc):
        return drop(Reason.AUTH_FAIL)

    # d: text-only MIME, size limits.
    bodies = mime.inspect_bodies(msg, len(raw))
    if isinstance(bodies, Reason):
        return drop(bodies)

    # e: deny-list on subject, subject pattern, template, deny-list on body minus description.
    subject = clean_text(str(msg.get("Subject", "")))
    if contains_denied(subject):
        return drop(Reason.DENYLIST)
    if bank.subject_re.fullmatch(subject) is None:
        return drop(Reason.SUBJECT_MISMATCH)
    try:
        parsed = khan_v1.parse(mime.body_rows(bodies))
    except (khan_v1.TemplateMismatchError, mime.MimeError):
        return drop(Reason.TEMPLATE_MISMATCH)
    if contains_denied(parsed.non_description_text):
        return drop(Reason.DENYLIST)

    # f: the credited account is the tenant's; transfers between own accounts are skipped.
    to_match = lookup_account(parsed.to_account.reveal())
    if to_match is None:
        return drop(Reason.NOT_TENANT_ACCOUNT)
    if not to_match.active:
        return drop(Reason.ACCOUNT_INACTIVE)
    if not include_own_transfers and lookup_account(parsed.from_account.reveal()) is not None:
        return drop(Reason.OWN_TRANSFER)

    # g: debit and credit agree, positive amount.
    if (
        parsed.from_amount != parsed.to_amount
        or parsed.from_currency != parsed.to_currency
        or parsed.to_amount <= 0
    ):
        return drop(Reason.AMOUNT_MISMATCH)

    return Accepted(
        msgid_sha256=msgid,
        account_id=to_match.account_id,
        to_account=parsed.to_account,
        journal_no=parsed.journal_no,
        system_date=parsed.system_date,
        amount=parsed.to_amount,
        currency=parsed.to_currency,
        txn_at=parsed.txn_at,
        description=sanitize_description(parsed.description_raw),
        parser_version=khan_v1.VERSION,
    )
