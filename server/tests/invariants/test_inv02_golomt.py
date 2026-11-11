"""SPEC §3 invariants for the Golomt "Easy Info" layout (``golomt_v1``).

Same rules as for Khan Bank, adapted to what this layout carries: only incoming transactions,
a masked credited account, no journal number (idempotency comes from the bank's DKIM signature),
no payer data, and a balance row that is never read.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.fixtures.emails import (
    TENANT_ACCOUNT,
    FakeGolomt,
    Mailer,
    build_golomt_message,
)

pytestmark = [pytest.mark.invariant(2)]

ACCOUNT_ID = uuid.UUID(int=7)
UB = ZoneInfo("Asia/Ulaanbaatar")


def accounts(active: bool = True, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    from khugjil.ingest.checks import AccountMatch

    found = {TENANT_ACCOUNT: AccountMatch(account_id=ACCOUNT_ID, active=active)}
    found.update(extra or {})
    return found


def evaluate(raw: bytes, mailer: Mailer, numbers: dict[str, Any] | None = None) -> Any:
    from khugjil.ingest.checks import BankConfig, evaluate, masked_match
    from tests.conftest import make_settings

    numbers = accounts() if numbers is None else numbers
    bank = BankConfig.from_settings(make_settings(bank_parser="golomt_v1"))

    def lookup(value: str) -> Any:
        return masked_match(value, numbers) if "*" in value else numbers.get(value)

    return evaluate(raw, bank=bank, lookup_account=lookup, dnsfunc=mailer.dns)


@pytest.mark.parametrize("fmt", ["plain", "html"])
@pytest.mark.parametrize("split", [True, False])
def test_income_is_accepted(mailer: Mailer, fmt: str, split: bool) -> None:
    from khugjil.ingest.checks import Accepted

    raw = mailer.golomt_mail(FakeGolomt(split_labels=split), fmt=fmt)
    r = evaluate(raw, mailer)
    assert isinstance(r, Accepted), r
    assert r.account_id == ACCOUNT_ID
    assert r.to_account.reveal() == TENANT_ACCOUNT  # the full registered number, not the mask
    assert (r.amount, r.currency) == (Decimal("2000.00"), "MNT")
    assert r.description == "Худалдан авалт"
    assert r.system_date == "2026-10-15"
    # The layout prints no time: it comes from the signed Date header, same day in Ulaanbaatar.
    assert r.txn_at == datetime(2026, 10, 15, 14, 5, 9, tzinfo=UB)
    assert r.parser_version == "golomt_v1"
    assert r.journal_no.startswith("dkim:")


def test_expense_notifications_are_skipped(mailer: Mailer) -> None:
    from khugjil.ingest.reasons import Reason

    raw = mailer.golomt_mail(FakeGolomt(kind="ЗАРЛАГЫН ГҮЙЛГЭЭ", amount="-2,000.00 MNT"))
    assert evaluate(raw, mailer).reason == Reason.NOT_INCOME


def test_date_header_on_another_day_falls_back_to_start_of_day(mailer: Mailer) -> None:
    raw = mailer.golomt_mail(date="Fri, 16 Oct 2026 00:30:00 +0800")
    assert evaluate(raw, mailer).txn_at == datetime(2026, 10, 15, 0, 0, tzinfo=UB)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"account": "333*****33"}, "NOT_TENANT_ACCOUNT"),
        ({"account": "2*********"}, "TEMPLATE_MISMATCH"),  # too few visible digits to match
        ({"account": "22*****22"}, "NOT_TENANT_ACCOUNT"),  # wrong length
        ({"amount": "2,000.00 MNT"}, "TEMPLATE_MISMATCH"),  # no "+": not an income line
        ({"amount": "+2000 MNT"}, "TEMPLATE_MISMATCH"),
        ({"amount": "+0.00 MNT"}, "AMOUNT_MISMATCH"),
        ({"date": "15.10.2026"}, "TEMPLATE_MISMATCH"),
        ({"balance": "Баярлалаа"}, "TEMPLATE_MISMATCH"),  # the balance row ends the description
        ({"kind": "ГҮЙЛГЭЭНИЙ МЭДЭЭ"}, "TEMPLATE_MISMATCH"),
        ({"footer": ("Нууц үгээ хэнд ч бүү хэлээрэй",)}, "DENYLIST"),
        ({"preamble": ("Таны нэг удаагийн код 123456",)}, "DENYLIST"),
    ],
)
def test_drops(mailer: Mailer, change: dict[str, Any], reason: str) -> None:
    raw = mailer.golomt_mail(FakeGolomt().with_(**change))
    assert str(evaluate(raw, mailer).reason) == reason


def test_inactive_and_ambiguous_accounts(mailer: Mailer) -> None:
    from khugjil.ingest.checks import AccountMatch

    raw = mailer.golomt_mail()
    assert str(evaluate(raw, mailer, accounts(active=False)).reason) == "ACCOUNT_INACTIVE"
    twin = {"2229999922": AccountMatch(account_id=uuid.uuid4(), active=True)}
    assert str(evaluate(raw, mailer, accounts(extra=twin)).reason) == "NOT_TENANT_ACCOUNT"


def test_sender_and_dkim_still_required(mailer: Mailer) -> None:
    unsigned = build_golomt_message()
    assert str(evaluate(unsigned, mailer).reason) == "AUTH_FAIL"
    forged = mailer.dkim_sign(build_golomt_message(), domain="attacker.example", selector="x1",
                              key="attacker")  # fmt: skip
    assert str(evaluate(forged, mailer).reason) == "AUTH_FAIL"


def test_description_cannot_move_other_fields(mailer: Mailer) -> None:
    from khugjil.ingest.checks import Accepted

    sneaky = "Гүйлгээний дүн: +9,999,999.00 MNT Дансны дугаар: 999*****99 нууц үг OTP"
    r = evaluate(mailer.golomt_mail(FakeGolomt(description=sneaky)), mailer)
    assert isinstance(r, Accepted)
    assert r.amount == Decimal("2000.00")
    assert r.account_id == ACCOUNT_ID
    assert r.description == sneaky  # deny-listed words inside the description are allowed


def test_balance_is_never_read(mailer: Mailer) -> None:
    """SPEC §3.4: an unmasked balance on the terminator row never reaches the result."""
    from khugjil.ingest.checks import Accepted

    balance = "Үлдэгдэл: 9,876,543.21 MNT"
    r = evaluate(mailer.golomt_mail(FakeGolomt(balance=balance)), mailer)
    assert isinstance(r, Accepted)
    assert "9,876,543.21" not in repr(r)
    assert "9876543" not in repr(r)


def test_idempotency_follows_the_bank_signature(mailer: Mailer) -> None:
    """SPEC §3.8 without a journal number: a replayed copy is the same transaction, a second
    genuine email with the same amount and text is a different one."""
    first = mailer.golomt_mail()
    again = evaluate(first, mailer)
    assert evaluate(first, mailer).journal_no == again.journal_no
    second = mailer.golomt_mail(date="Thu, 15 Oct 2026 14:06:40 +0800",
                                message_id="<fake-golomt-2@bank.example>")  # fmt: skip
    assert evaluate(second, mailer).journal_no != again.journal_no
    # Changing an unsigned header does not make a replay look new.
    replay = first.replace(b"\r\n\r\n", b"\r\nX-Replay: 1\r\n\r\n", 1)
    assert evaluate(replay, mailer).journal_no == again.journal_no


@pytest.mark.db
async def test_pipeline_stores_golomt_income_once(world: Any, mailer: Mailer) -> None:
    """End to end through the real pipeline: masked account matched against the encrypted
    registered number, stored once, replay counted as a duplicate, no balance anywhere."""
    from dataclasses import replace as dc_replace

    from khugjil.ingest.pipeline import ingest

    deps = world.db.deps(world._enqueue)
    deps = dc_replace(deps, settings=deps.settings.model_copy(update={"bank_parser": "golomt_v1"}))
    raw = mailer.golomt_mail(FakeGolomt(balance="Үлдэгдэл: 9,876,543.21 MNT"))
    first = await ingest(deps, world.inbound_address, raw)
    assert str(first.reason) == "ACCEPTED"
    assert str((await ingest(deps, world.inbound_address, raw)).reason) == "DUPLICATE"
    expense = mailer.golomt_mail(FakeGolomt(kind="ЗАРЛАГЫН ГҮЙЛГЭЭ", amount="-500.00 MNT"))
    assert str((await ingest(deps, world.inbound_address, expense)).reason) == "NOT_INCOME"

    rows = await world.rows("transaction")
    assert len(rows) == 1
    assert rows[0]["bank_account_id"] == world.account_id
    assert rows[0]["amount"] == Decimal("2000.00")
    assert rows[0]["parser_version"] == "golomt_v1"
    dump = await world.dump_all_text()
    assert "9,876,543.21" not in dump
    assert "9876543" not in dump
