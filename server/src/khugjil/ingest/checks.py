"""Checks b..g (SPEC §3.2) as one pure function, for every supported bank layout.

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
from datetime import datetime, time
from decimal import Decimal
from email.message import Message
from email.utils import parsedate_to_datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from khugjil.crypto import sha256_hex
from khugjil.ingest import mime
from khugjil.ingest.denylist import contains_denied
from khugjil.ingest.dkim_arc import DnsFunc, authenticate, bank_signature_id
from khugjil.ingest.forwarding import confirmation_code, sender_address
from khugjil.ingest.parsers import golomt_v1, khan_v1
from khugjil.ingest.reasons import Reason
from khugjil.ingest.sanitize import clean_text, sanitize_description
from khugjil.sensitive import Sensitive
from khugjil.settings import BANK_TZ, Settings

SINGLETON_HEADERS = ("From", "Subject", "Date", "To")
_TZ = ZoneInfo(BANK_TZ)


@dataclass(frozen=True)
class BankConfig:
    senders: frozenset[str]
    dkim_domains: frozenset[str]
    subject_re: re.Pattern[str]
    parser: str = khan_v1.VERSION

    @classmethod
    def from_settings(cls, settings: Settings) -> BankConfig:
        return cls(
            senders=frozenset(settings.bank_senders),
            dkim_domains=frozenset(settings.bank_dkim_domains),
            subject_re=settings.subject_pattern,
            parser=settings.bank_parser,
        )


@dataclass(frozen=True)
class AccountMatch:
    account_id: UUID
    active: bool
    # The registered (full, canonical) number, set when the email showed a masked number.
    canonical: Sensitive | None = None


AccountLookup = Callable[[str], AccountMatch | None]

MIN_VISIBLE_DIGITS = 4


def masked_match(masked: str, accounts: dict[str, AccountMatch]) -> AccountMatch | None:
    """Match a masked number ("123*****89") against the shop's full numbers: same length, equal
    at every visible digit. Exactly one candidate, or None (unknown or ambiguous)."""
    visible = [(i, c) for i, c in enumerate(masked) if c != "*"]
    if len(visible) < MIN_VISIBLE_DIGITS or not all(c.isdigit() for _, c in visible):
        return None
    hits = [
        (number, match)
        for number, match in accounts.items()
        if len(number) == len(masked) and all(number[i] == c for i, c in visible)
    ]
    if len(hits) != 1:
        return None
    number, match = hits[0]
    return AccountMatch(match.account_id, match.active, Sensitive(number))


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


@dataclass(frozen=True)
class Income:
    """What any bank parser hands to checks f and g (fields a layout lacks are None)."""

    to_account: Sensitive  # canonical, or masked with "*"
    amount: Decimal
    currency: str
    txn_at: datetime
    system_date: str
    journal_no: str | None  # None: the bank prints no transaction number
    from_account: Sensitive | None  # None: the bank shows no payer/debit side
    from_amount: Decimal | None
    from_currency: str | None
    description_raw: str
    non_description_text: str
    parser_version: str


class NotIncomeError(Exception):
    pass


def _khan(rows: mime.Rows, msg: Message) -> Income:
    p = khan_v1.parse(rows)
    return Income(
        to_account=p.to_account,
        amount=p.to_amount,
        currency=p.to_currency,
        txn_at=p.txn_at,
        system_date=p.system_date,
        journal_no=p.journal_no,
        from_account=p.from_account,
        from_amount=p.from_amount,
        from_currency=p.from_currency,
        description_raw=p.description_raw,
        non_description_text=p.non_description_text,
        parser_version=khan_v1.VERSION,
    )


def _golomt(rows: mime.Rows, msg: Message) -> Income:
    try:
        p = golomt_v1.parse(rows)
    except golomt_v1.NotIncomeError as exc:
        raise NotIncomeError from exc
    return Income(
        to_account=p.account_masked,
        amount=p.amount,
        currency=p.currency,
        txn_at=_golomt_time(p, msg),
        system_date=p.date,
        journal_no=None,
        from_account=None,
        from_amount=None,
        from_currency=None,
        description_raw=p.description_raw,
        non_description_text=p.non_description_text,
        parser_version=golomt_v1.VERSION,
    )


def _golomt_time(p: golomt_v1.ParsedGolomt, msg: Message) -> datetime:
    """The printed date has no time: take it from the (DKIM-signed) Date header when that is the
    same day in Ulaanbaatar, otherwise the start of the printed day."""
    try:
        day = datetime.strptime(p.date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise golomt_v1.TemplateMismatchError("bad date") from exc
    if p.time:
        fmt = "%H:%M:%S" if p.time.count(":") == 2 else "%H:%M"
        try:
            return datetime.combine(day, datetime.strptime(p.time, fmt).time(), _TZ)
        except ValueError as exc:
            raise golomt_v1.TemplateMismatchError("bad time") from exc
    try:
        sent = parsedate_to_datetime(str(msg.get("Date", ""))).astimezone(_TZ)
    except (TypeError, ValueError):
        sent = None
    if sent is not None and sent.date() == day:
        return sent.replace(microsecond=0)
    return datetime.combine(day, time(0), _TZ)


PARSERS: dict[str, Callable[[mime.Rows, Message], Income]] = {
    khan_v1.VERSION: _khan,
    golomt_v1.VERSION: _golomt,
}
TEMPLATE_ERRORS = (khan_v1.TemplateMismatchError, golomt_v1.TemplateMismatchError, mime.MimeError)


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
        parsed = PARSERS[bank.parser](mime.body_rows(bodies), msg)
    except NotIncomeError:
        return drop(Reason.NOT_INCOME)
    except TEMPLATE_ERRORS:
        return drop(Reason.TEMPLATE_MISMATCH)
    if contains_denied(parsed.non_description_text):
        return drop(Reason.DENYLIST)

    # f: the credited account is the tenant's; transfers between own accounts are skipped
    # (only possible when the bank shows the debited account).
    to_match = lookup_account(parsed.to_account.reveal())
    if to_match is None:
        return drop(Reason.NOT_TENANT_ACCOUNT)
    if not to_match.active:
        return drop(Reason.ACCOUNT_INACTIVE)
    if (
        parsed.from_account is not None
        and not include_own_transfers
        and lookup_account(parsed.from_account.reveal()) is not None
    ):
        return drop(Reason.OWN_TRANSFER)

    # g: positive amount; where the bank shows both sides, debit and credit agree.
    if parsed.amount <= 0:
        return drop(Reason.AMOUNT_MISMATCH)
    if parsed.from_amount is not None and (
        parsed.from_amount != parsed.amount or parsed.from_currency != parsed.currency
    ):
        return drop(Reason.AMOUNT_MISMATCH)

    # Idempotency material: the bank's transaction number, or (layouts without one) the bank's
    # own DKIM signature, which a replayed or re-forwarded copy cannot change.
    journal = parsed.journal_no
    if journal is None:
        signature = bank_signature_id(raw, bank.dkim_domains)
        if signature is None:
            return drop(Reason.AUTH_FAIL)
        journal = f"dkim:{signature}"

    return Accepted(
        msgid_sha256=msgid,
        account_id=to_match.account_id,
        to_account=to_match.canonical or parsed.to_account,
        journal_no=journal,
        system_date=parsed.system_date,
        amount=parsed.amount,
        currency=parsed.currency,
        txn_at=parsed.txn_at,
        description=sanitize_description(parsed.description_raw),
        parser_version=parsed.parser_version,
    )
