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

Rows are matched in a fixed order; a label's value may sit in the same cell, the next cell or the
next row (HTML tables and plain text normalize differently). The description is captured LAST,
strictly between its label and the balance row.
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
_AMOUNT_LABEL = re.compile(r"Гүйлгээний дүн:\s*(.*)")
_AMOUNT = re.compile(r"\+\s?(\d{1,3}(?:,\d{3})*\.\d{2})\s?([A-Z]{3})")
_DATE_LABEL = re.compile(r"Гүйлгээний огноо:\s*(.*)")
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2}(?::\d{2})?))?")
_ACCOUNT_LABEL = re.compile(r"Дансны дугаар:\s*(.*)")
_ACCOUNT = re.compile(r"[0-9*]{6,34}")
_DESC_LABEL = re.compile(r"Гүйлгээний утга:\s?(.*)")
_TERMINATOR = re.compile(r"Үлдэгдэл:.*")


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


class _Cursor:
    def __init__(self, rows: list[list[str]], start: int) -> None:
        self.rows = rows
        self.i = start

    def take(self) -> list[str]:
        if self.i >= len(self.rows):
            raise TemplateMismatchError("unexpected end")
        row = self.rows[self.i]
        self.i += 1
        return row

    def labeled(self, label: re.Pattern[str], value: re.Pattern[str]) -> re.Match[str]:
        """``Label: value`` in one row, or the label alone with the value in the next row."""
        m = label.fullmatch(_text(self.take()))
        if m is None:
            raise TemplateMismatchError("label not found")
        raw = m.group(1).strip() or _text(self.take())
        v = value.fullmatch(raw)
        if v is None:
            raise TemplateMismatchError("bad value")
        return v


def parse(rows: list[list[str]]) -> ParsedGolomt:
    start = None
    for i, row in enumerate(rows[: MAX_PREAMBLE_ROWS + 1]):
        kind = _text(row).casefold()
        if kind == _EXPENSE:
            raise NotIncomeError
        if kind == _INCOME:
            start = i
            break
    if start is None:
        raise TemplateMismatchError("header row not found")
    cur = _Cursor(rows, start + 1)

    amount_m = cur.labeled(_AMOUNT_LABEL, _AMOUNT)
    amount = Decimal(amount_m.group(1).replace(",", ""))
    currency = amount_m.group(2)
    date_m = cur.labeled(_DATE_LABEL, _DATE)
    account_m = cur.labeled(_ACCOUNT_LABEL, _ACCOUNT)
    if sum(c.isdigit() for c in account_m.group(0)) < 4:
        raise TemplateMismatchError("too few visible account digits")

    label_row = cur.take()
    inline_m = _DESC_LABEL.fullmatch(_text(label_row))
    if inline_m is None:
        raise TemplateMismatchError("description label")
    inline = inline_m.group(1).strip()
    template_end = cur.i
    desc_parts = [inline] if inline else []
    while True:
        if cur.i >= len(rows):
            raise TemplateMismatchError("balance row not found")
        if _TERMINATOR.fullmatch(_text(rows[cur.i])):
            break
        desc_parts.append(_text(cur.take()))
    desc_end = cur.i

    outside = rows[:template_end] + rows[desc_end:]
    if inline:
        outside[template_end - 1] = ["Гүйлгээний утга:"]
    return ParsedGolomt(
        account_masked=Sensitive(account_m.group(0)),
        amount=amount,
        currency=currency,
        date=date_m.group(1),
        time=date_m.group(2),
        description_raw=" ".join(desc_parts),
        non_description_text="\n".join(_text(r) for r in outside),
    )
