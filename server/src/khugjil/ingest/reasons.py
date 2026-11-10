from __future__ import annotations

from enum import StrEnum


class Reason(StrEnum):
    """Outcome codes: the only thing recorded about an email (with tenant + msgid hash)."""

    RCPT_UNKNOWN = "RCPT_UNKNOWN"  # a
    FWD_CONFIRM_STORED = "FWD_CONFIRM_STORED"
    HEADER_DUPLICATE = "HEADER_DUPLICATE"  # b
    FROM_NOT_ALLOWED = "FROM_NOT_ALLOWED"  # b
    AUTH_FAIL = "AUTH_FAIL"  # c
    ATTACHMENT = "ATTACHMENT"  # d
    TOO_LARGE = "TOO_LARGE"  # d
    MIME_INVALID = "MIME_INVALID"  # d
    DENYLIST = "DENYLIST"  # e
    SUBJECT_MISMATCH = "SUBJECT_MISMATCH"  # e
    TEMPLATE_MISMATCH = "TEMPLATE_MISMATCH"  # e
    NOT_TENANT_ACCOUNT = "NOT_TENANT_ACCOUNT"  # f
    ACCOUNT_INACTIVE = "ACCOUNT_INACTIVE"  # f
    OWN_TRANSFER = "OWN_TRANSFER"  # f
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"  # g
    DUPLICATE = "DUPLICATE"
    ACCEPTED = "ACCEPTED"


# Codes that suggest the bank changed its format (sender and DKIM were valid).
TEMPLATE_CHANGE_REASONS = frozenset({Reason.SUBJECT_MISMATCH, Reason.TEMPLATE_MISMATCH})
