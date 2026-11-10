"""Notification content, identical on every channel (SPEC §7). Plain text only, no emoji."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from khugjil.settings import BANK_TZ

_TZ = ZoneInfo(BANK_TZ)
_CENT = Decimal("0.01")


def format_amount(amount: Decimal, currency: str) -> str:
    """``1,234,567 ₮``; decimals only when non-zero; other currencies use their ISO code."""
    text = f"{amount.quantize(_CENT, rounding=ROUND_HALF_UP):,.2f}"
    if text.endswith(".00"):
        text = text[:-3]
    return f"{text} ₮" if currency == "MNT" else f"{text} {currency}"


def format_time(at: datetime) -> str:
    return at.astimezone(_TZ).strftime("%H:%M")


def format_datetime(at: datetime) -> str:
    return at.astimezone(_TZ).strftime("%Y.%m.%d %H:%M")


@dataclass(frozen=True)
class Notification:
    transaction_id: UUID
    seq: int
    amount: Decimal
    currency: str
    amount_text: str
    description: str
    txn_at: datetime
    time_text: str
    datetime_text: str
    account_alias: str | None
    test: bool = False

    def lines(self) -> list[str]:
        out = ["Туршилт"] if self.test else []
        out += ["Орлого", self.amount_text]
        if self.description:
            out.append(f"Утга: {self.description}")
        out.append(self.datetime_text)
        if self.account_alias:
            out.append(self.account_alias)
        return out

    def plain_text(self) -> str:
        return "\n".join(self.lines())

    def to_api(self) -> dict[str, object]:
        return {
            "id": str(self.transaction_id),
            "seq": self.seq,
            "amount": str(self.amount),
            "currency": self.currency,
            "amount_text": self.amount_text,
            "description": self.description,
            "txn_at": self.txn_at.isoformat(),
            "time_text": self.time_text,
            "datetime_text": self.datetime_text,
            "account_alias": self.account_alias,
            "test": self.test,
        }


def build_notification(
    *,
    transaction_id: UUID,
    seq: int,
    amount: Decimal,
    currency: str,
    txn_at: datetime,
    description: str,
    account_alias: str | None,
    show_alias: bool,
    test: bool = False,
) -> Notification:
    return Notification(
        transaction_id=transaction_id,
        seq=seq,
        amount=amount,
        currency=currency,
        amount_text=format_amount(amount, currency),
        description=description,
        txn_at=txn_at,
        time_text=format_time(txn_at),
        datetime_text=format_datetime(txn_at),
        account_alias=account_alias if show_alias else None,
        test=test,
    )
