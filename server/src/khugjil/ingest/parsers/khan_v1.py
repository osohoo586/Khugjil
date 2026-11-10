"""Parser ``khan_v1`` for the SPEC §4 notification layout.

Sections are matched in a fixed, anchored order on normalized rows of cells. The description
is captured LAST, strictly between its label row and the "Харилцагч танд баярлалаа" row, so
label-like text inside it cannot influence any other field. Payer name, seller name, rate,
amount in words, bank name and anything after the terminator (e.g. a balance line) are matched
without being captured.

Built against the fake SPEC §4 sample. The real layout (PLAN Q6, Q8, Q9, Q11) may require a
``khan_v2``; the version that parsed each transaction is stored with it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from khugjil.sensitive import Sensitive
from khugjil.settings import BANK_TZ

VERSION = "khan_v1"
MAX_PREAMBLE_ROWS = 10
_TZ = ZoneInfo(BANK_TZ)
_DT = r"(\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2})"
_DT_FMT = "%Y.%m.%d %H:%M:%S"

_DATE = re.compile(rf"Огноо/Date:\s?{_DT}")
_INFO = "шилжүүлгийн мэдээлэл/transaction information"
_JOURNAL = re.compile(r"Журналын/Journal No:\s?(\d{1,20})")
_SYSDATE = re.compile(rf"Системийн огноо/System Date:\s?{_DT}")
_FROM_HEADER = (
    "дт/from",
    "iban/дансны/картын дугаар iban/account/card number",
    "нэр/name",
    "дүн/amount",
    "ханш/rate",
)
_TO_LABEL = re.compile(r"[KК]т/To")
_TO_HEADER_TAIL = ("банкны дугаар/branch no", "банкны нэр/bank name")
_TO_ACCOUNT_HEADER = ("iban/дансны/картын дугаар iban/account/card number", "нэр/name")
_ACCOUNT = re.compile(r"[0-9A-Z*]{6,34}")
_AMOUNT = re.compile(r"\d{1,3}(?:,\d{3})*\.\d{2}")
_CURRENCY = re.compile(r"[A-Z]{3}")
_RATE = re.compile(r"\d{1,9}(?:\.\d{1,6})?")
_WORDS = re.compile(r"/ .{1,300} /")
_BRANCH = re.compile(r"\d{1,4}")
_DESC_LABEL = re.compile(r"Гүйлгээний утга/Transaction description:\s?(.*)")
_TERMINATOR = re.compile(r"Харилцагч танд баярлалаа\.?/Thank you to our customers\.?")


class TemplateMismatchError(Exception):
    pass


@dataclass(frozen=True)
class ParsedTemplate:
    txn_at: datetime
    journal_no: str
    system_date: str
    from_account: Sensitive
    from_amount: Decimal
    from_currency: str
    to_account: Sensitive
    to_amount: Decimal
    to_currency: str
    description_raw: str
    non_description_text: str


def canonical_account(value: str) -> str:
    """Canonical form shared by email parsing and admin input (PLAN Q9 may refine it)."""
    return re.sub(r"[\s-]", "", value).upper()


def _fold(cells: list[str]) -> tuple[str, ...]:
    return tuple(c.casefold() for c in cells)


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

    def peek(self) -> list[str] | None:
        return self.rows[self.i] if self.i < len(self.rows) else None


def _single(row: list[str], pattern: re.Pattern[str]) -> re.Match[str]:
    if len(row) != 1:
        raise TemplateMismatchError("expected one cell")
    m = pattern.fullmatch(row[0])
    if m is None:
        raise TemplateMismatchError("row did not match")
    return m


def _amount(cell: str) -> Decimal:
    if not _AMOUNT.fullmatch(cell):
        raise TemplateMismatchError("bad amount")
    return Decimal(cell.replace(",", ""))


def _currency(cell: str) -> str:
    if not _CURRENCY.fullmatch(cell):
        raise TemplateMismatchError("bad currency")
    return cell


def _account(cell: str) -> Sensitive:
    canon = canonical_account(cell)
    if not _ACCOUNT.fullmatch(canon):
        raise TemplateMismatchError("bad account")
    return Sensitive(canon)


def _datetime(value: str) -> datetime:
    try:
        return datetime.strptime(value, _DT_FMT).replace(tzinfo=_TZ)
    except ValueError as exc:
        raise TemplateMismatchError("bad datetime") from exc


def _amount_row(cells: list[str]) -> tuple[Decimal, str]:
    *_, amount, currency, rate = cells
    if not _RATE.fullmatch(rate):
        raise TemplateMismatchError("bad rate")
    return _amount(amount), _currency(currency)


def parse(rows: list[list[str]]) -> ParsedTemplate:
    start = next(
        (
            i
            for i, r in enumerate(rows[: MAX_PREAMBLE_ROWS + 1])
            if len(r) == 1 and _DATE.fullmatch(r[0])
        ),
        None,
    )
    if start is None:
        raise TemplateMismatchError("date row not found")
    cur = _Cursor(rows, start)

    txn_at = _datetime(_single(cur.take(), _DATE).group(1))
    info = cur.take()
    if _fold(info) != (_INFO,):
        raise TemplateMismatchError("info row")
    journal = _single(cur.take(), _JOURNAL).group(1)
    system_date = _single(cur.take(), _SYSDATE).group(1)
    _datetime(system_date)

    if _fold(cur.take()) != _FROM_HEADER:
        raise TemplateMismatchError("from header")
    from_row = cur.take()
    if len(from_row) not in (4, 5):
        raise TemplateMismatchError("from row")
    from_account = _account(from_row[0])
    from_amount, from_currency = _amount_row(from_row)

    nxt = cur.peek()
    if nxt is not None and len(nxt) == 1 and _WORDS.fullmatch(nxt[0]):
        cur.take()

    to_header = cur.take()
    if (
        len(to_header) != 6
        or not _TO_LABEL.fullmatch(to_header[0])
        or _fold(to_header[1:3]) != _TO_HEADER_TAIL
    ):
        raise TemplateMismatchError("to header")
    to_amount, to_currency = _amount_row(to_header)

    branch = cur.take()
    if len(branch) != 2 or not _BRANCH.fullmatch(branch[0]):
        raise TemplateMismatchError("branch row")
    if _fold(cur.take()) != _TO_ACCOUNT_HEADER:
        raise TemplateMismatchError("to account header")
    to_row = cur.take()
    if len(to_row) not in (1, 2):
        raise TemplateMismatchError("to account row")
    to_account = _account(to_row[0])

    label_row = cur.take()
    inline = _single(label_row, _DESC_LABEL).group(1)
    template_end = cur.i

    desc_parts = [inline] if inline else []
    while True:
        row = cur.peek()
        if row is None:
            raise TemplateMismatchError("terminator not found")
        if len(row) == 1 and _TERMINATOR.fullmatch(row[0]):
            break
        desc_parts.append(" ".join(cur.take()))
    desc_end = cur.i

    outside = rows[:template_end] + rows[desc_end:]
    if inline:
        outside[template_end - 1] = ["Гүйлгээний утга/Transaction description:"]
    non_description = "\n".join(" ".join(r) for r in outside)

    return ParsedTemplate(
        txn_at=txn_at,
        journal_no=journal,
        system_date=system_date,
        from_account=from_account,
        from_amount=from_amount,
        from_currency=from_currency,
        to_account=to_account,
        to_amount=to_amount,
        to_currency=to_currency,
        description_raw=" ".join(desc_parts),
        non_description_text=non_description,
    )
