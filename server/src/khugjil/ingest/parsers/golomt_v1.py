"""Parser ``golomt_v1`` for Golomt Bank "Easy Info" notification emails.

Built from the layout the shop owner described on 2026-11-11 (values here are fake):

    ОРЛОГЫН ГҮЙЛГЭЭ
    Гүйлгээний дүн:
    +2,000.00 MNT
    Гүйлгээний огноо:2026-11-11
    Дансны дугаар: 123*****89
    Гүйлгээний утга: ХУДАЛДАН АВАЛТ
    Үлдэгдэл: *******

Differences from ``khan_v1`` that the checks account for:

- Easy Info also reports expenses ("ЗАРЛАГЫН ГҮЙЛГЭЭ"); those raise ``NotIncomeError``.
- The credited account is masked; it is matched digit by digit against the shop's accounts.
- There is no journal number, no payer and no debit side (so no own-transfer or debit/credit
  comparison is possible). The date has no time of day.
- The balance row ends the description and is never read; payer data is never present.

The labelled fields are matched in a fixed order on the normalized text, wherever the
HTML puts its line breaks: in the real email (2026-11-11, seen as structure only) the account,
description and balance share one row, while the plain-text form puts each on its own line.
The description is captured LAST, strictly between its label and the final balance label, so
label-like text inside it cannot move any other field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from khugjil.sensitive import Sensitive

VERSION = "golomt_v1"
MAX_PREAMBLE_ROWS = 10

_INCOME = "орлогын гүйлгээ"
_EXPENSE = "зарлагын гүйлгээ"
_TEMPLATE = re.compile(
    r"ОРЛОГЫН ГҮЙЛГЭЭ\s+"
    r"Гүйлгээний дүн:\s*\+\s?(?P<amount>\d{1,3}(?:,\d{3})*\.\d{2})\s?(?P<currency>[A-Z]{3})\s+"
    r"Гүйлгээний огноо:\s*(?P<date>\d{4}-\d{2}-\d{2})(?:[ T](?P<time>\d{2}:\d{2}(?::\d{2})?))?\s+"
    r"Дансны дугаар:\s*(?P<account>[0-9*]{6,34})\s+"
    r"Гүйлгээний утга:(?P<desc>.*)"
    r"Үлдэгдэл:[^\n]*(?:\n|$)",
    re.DOTALL,
)


class TemplateMismatchError(Exception):
    pass


class NotIncomeError(Exception):
    """An Easy Info expense notification: not an error, just not ours to show."""


@dataclass(frozen=True)
class ParsedGolomt:
    account_masked: Sensitive
    amount: Decimal
    currency: str
    date: str  # YYYY-MM-DD as printed
    time: str | None  # HH:MM[:SS] when the bank prints one
    description_raw: str
    non_description_text: str


def _text(row: list[str]) -> str:
    return " ".join(row).strip()


def parse(rows: list[list[str]]) -> ParsedGolomt:
    head = [_text(r).casefold() for r in rows[: MAX_PREAMBLE_ROWS + 1]]
    if _EXPENSE in head:
        raise NotIncomeError
    if _INCOME not in head:
        raise TemplateMismatchError("header row not found")
    text = "\n".join(_text(r) for r in rows)
    limit = sum(len(_text(r)) + 1 for r in rows[: head.index(_INCOME)])
    m = _TEMPLATE.search(text)
    if m is None or m.start() != limit:
        raise TemplateMismatchError("fields not in the expected order")
    account = m.group("account")
    if sum(c.isdigit() for c in account) < 4:
        raise TemplateMismatchError("too few visible account digits")
    desc_start, desc_end = m.span("desc")
    return ParsedGolomt(
        account_masked=Sensitive(account),
        amount=Decimal(m.group("amount").replace(",", "")),
        currency=m.group("currency"),
        date=m.group("date"),
        time=m.group("time"),
        description_raw=" ".join(m.group("desc").split()),
        non_description_text=text[:desc_start] + "\n" + text[desc_end:],
    )
