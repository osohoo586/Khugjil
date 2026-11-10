"""SPEC §3.2: process an email only if checks a..g all pass, in order; log only 3 fields."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from tests.fixtures.emails import (
    PAYER_ACCOUNT,
    TENANT_ACCOUNT_2,
    FakeTxn,
    Mailer,
    add_header_after_signing,
    append_to_body,
    break_dkim,
    build_message,
)
from tests.invariants.helpers import evaluate, two_accounts

pytestmark = pytest.mark.invariant(2)


def reason(result: object) -> str:
    from khugjil.ingest.checks import Accepted

    return "ACCEPTED" if isinstance(result, Accepted) else str(result.reason)  # type: ignore[attr-defined]


# ---------------------------------------------------------------- happy paths


def test_accepts_dkim_signed_bank_mail(mailer: Mailer) -> None:
    r = evaluate(mailer.bank_mail(), mailer)
    assert reason(r) == "ACCEPTED"
    assert r.amount == Decimal("2800.00")
    assert r.currency == "MNT"
    assert r.description == "Худалдан авалт"
    assert r.txn_at == datetime(2026, 10, 15, 8, 37, 22, tzinfo=ZoneInfo("Asia/Ulaanbaatar"))
    assert r.parser_version == "khan_v1"


@pytest.mark.parametrize("fmt", ["html", "plain", "both"])
def test_accepts_each_body_format(mailer: Mailer, fmt: str) -> None:
    assert reason(evaluate(mailer.bank_mail(fmt=fmt), mailer)) == "ACCEPTED"


def test_accepts_gmail_arc_when_forwarding_broke_dkim(mailer: Mailer) -> None:
    raw = mailer.gmail_forward(break_dkim(mailer.bank_mail()))
    assert reason(evaluate(raw, mailer)) == "ACCEPTED"


def test_accepts_arc_with_header_d_syntax(mailer: Mailer) -> None:
    raw = mailer.gmail_forward(
        break_dkim(mailer.bank_mail()), aar="dkim=pass header.d=bank.example header.s=s1"
    )
    assert reason(evaluate(raw, mailer)) == "ACCEPTED"


def test_latin_k_in_kt_label_is_normalized(mailer: Mailer) -> None:
    raw = mailer.bank_mail(FakeTxn(to_label="Kт/To"))
    assert reason(evaluate(raw, mailer)) == "ACCEPTED"


# ---------------------------------------------------------------- b: sender


def test_b_sender_not_in_allowlist(mailer: Mailer) -> None:
    raw = mailer.bank_mail(from_addr="notify@attacker.example")
    assert reason(evaluate(raw, mailer)) == "FROM_NOT_ALLOWED"


def test_b_display_name_spoof(mailer: Mailer) -> None:
    raw = mailer.bank_mail(from_addr='"notify@bank.example" <evil@attacker.example>')
    assert reason(evaluate(raw, mailer)) == "FROM_NOT_ALLOWED"


@pytest.mark.parametrize("header", ["From", "Subject", "Date", "To"])
def test_b_duplicate_signed_headers_rejected(mailer: Mailer, header: str) -> None:
    raw = add_header_after_signing(mailer.bank_mail(), header, "notify@bank.example")
    assert reason(evaluate(raw, mailer)) == "HEADER_DUPLICATE"


# ---------------------------------------------------------------- c: authentication


def test_c_unsigned(mailer: Mailer) -> None:
    assert reason(evaluate(build_message(), mailer)) == "AUTH_FAIL"


def test_c_signed_by_other_domain(mailer: Mailer) -> None:
    raw = mailer.dkim_sign(
        build_message(), domain="attacker.example", selector="x1", key="attacker"
    )
    assert reason(evaluate(raw, mailer)) == "AUTH_FAIL"


def test_c_bank_domain_with_unpublished_key(mailer: Mailer) -> None:
    raw = mailer.dkim_sign(build_message(), selector="x1", key="attacker")
    assert reason(evaluate(raw, mailer)) == "AUTH_FAIL"


def test_c_broken_dkim_without_arc(mailer: Mailer) -> None:
    assert reason(evaluate(break_dkim(mailer.bank_mail()), mailer)) == "AUTH_FAIL"


def test_c_body_length_tag_rejected(mailer: Mailer) -> None:
    raw = mailer.dkim_sign(build_message(), length=True)
    assert reason(evaluate(raw, mailer)) == "AUTH_FAIL"
    appended = append_to_body(raw, b"--injected")
    assert reason(evaluate(appended, mailer)) == "AUTH_FAIL"


def test_c_signature_must_cover_subject(mailer: Mailer) -> None:
    raw = mailer.dkim_sign(build_message(), headers=[b"from", b"date"])
    assert reason(evaluate(raw, mailer)) == "AUTH_FAIL"


def test_c_arc_sealed_by_non_google(mailer: Mailer) -> None:
    raw = mailer.gmail_forward(
        break_dkim(mailer.bank_mail()), sealer="attacker.example", selector="x1", key="attacker"
    )
    assert reason(evaluate(raw, mailer)) == "AUTH_FAIL"


@pytest.mark.parametrize(
    "aar",
    [
        "dkim=fail header.i=@bank.example",
        "dkim=pass header.i=@attacker.example",
        "dkim=pass header.i=@sub.bank.example",
        "spf=pass smtp.mailfrom=bank.example",
    ],
)
def test_c_arc_results_must_record_bank_dkim_pass(mailer: Mailer, aar: str) -> None:
    raw = mailer.gmail_forward(break_dkim(mailer.bank_mail()), aar=aar)
    assert reason(evaluate(raw, mailer)) == "AUTH_FAIL"


def test_c_arc_results_from_other_authserv(mailer: Mailer) -> None:
    raw = mailer.gmail_forward(break_dkim(mailer.bank_mail()), srv_id="mx.attacker.example")
    assert reason(evaluate(raw, mailer)) == "AUTH_FAIL"


# ---------------------------------------------------------------- d: shape


def test_d_attachment(mailer: Mailer) -> None:
    assert reason(evaluate(mailer.bank_mail(attachment=True), mailer)) == "ATTACHMENT"


def test_d_oversized(mailer: Mailer) -> None:
    big = FakeTxn(footer=("x" * 300_000,))
    assert reason(evaluate(mailer.bank_mail(big), mailer)) == "TOO_LARGE"


# ---------------------------------------------------------------- e: subject + template


def test_e_subject_mismatch(mailer: Mailer) -> None:
    raw = mailer.bank_mail(subject="Monthly statement")
    assert reason(evaluate(raw, mailer)) == "SUBJECT_MISMATCH"


def test_e_template_mismatch(mailer: Mailer) -> None:
    raw = mailer.bank_mail(body_override="<p>Hello</p>")
    assert reason(evaluate(raw, mailer)) == "TEMPLATE_MISMATCH"


def test_e_unexpected_row_inside_template(mailer: Mailer) -> None:
    t = FakeTxn(extra_rows_after_journal=(("Дүн/Amount:9,999,999.00",),))
    assert reason(evaluate(mailer.bank_mail(t), mailer)) == "TEMPLATE_MISMATCH"


# ---------------------------------------------------------------- f: accounts


def test_f_to_account_not_registered(mailer: Mailer) -> None:
    raw = mailer.bank_mail(FakeTxn(to_account="9999999999"))
    assert reason(evaluate(raw, mailer)) == "NOT_TENANT_ACCOUNT"


def test_f_inactive_account(mailer: Mailer) -> None:
    from khugjil.ingest.checks import AccountMatch
    from tests.invariants.helpers import ACCOUNT_ID

    accounts = {"2222222222": AccountMatch(account_id=ACCOUNT_ID, active=False)}
    assert reason(evaluate(mailer.bank_mail(), mailer, accounts=accounts)) == "ACCOUNT_INACTIVE"


def test_f_own_transfer_skipped_by_default(mailer: Mailer) -> None:
    raw = mailer.bank_mail(FakeTxn(from_account=TENANT_ACCOUNT_2))
    assert reason(evaluate(raw, mailer, accounts=two_accounts())) == "OWN_TRANSFER"
    r = evaluate(raw, mailer, accounts=two_accounts(), include_own_transfers=True)
    assert reason(r) == "ACCEPTED"


# ---------------------------------------------------------------- g: amounts


@pytest.mark.parametrize(
    "txn",
    [
        FakeTxn(to_amount="2,900.00"),
        FakeTxn(to_currency="USD"),
        FakeTxn(from_amount="0.00", to_amount="0.00"),
    ],
)
def test_g_amount_checks(mailer: Mailer, txn: FakeTxn) -> None:
    assert reason(evaluate(mailer.bank_mail(txn), mailer)) == "AMOUNT_MISMATCH"


# ---------------------------------------------------------------- ordering


def test_order_b_before_c(mailer: Mailer) -> None:
    raw = build_message(from_addr="evil@attacker.example")  # unsigned too
    assert reason(evaluate(raw, mailer)) == "FROM_NOT_ALLOWED"


def test_order_c_before_d(mailer: Mailer) -> None:
    raw = build_message(attachment=True)  # unsigned too
    assert reason(evaluate(raw, mailer)) == "AUTH_FAIL"


def test_order_d_before_e(mailer: Mailer) -> None:
    raw = mailer.bank_mail(attachment=True, subject="Monthly statement")
    assert reason(evaluate(raw, mailer)) == "ATTACHMENT"


def test_order_e_before_f(mailer: Mailer) -> None:
    raw = mailer.bank_mail(FakeTxn(to_account="9999999999"), subject="Monthly statement")
    assert reason(evaluate(raw, mailer)) == "SUBJECT_MISMATCH"


def test_order_f_before_g(mailer: Mailer) -> None:
    raw = mailer.bank_mail(FakeTxn(to_account="9999999999", to_amount="1.00"))
    assert reason(evaluate(raw, mailer)) == "NOT_TENANT_ACCOUNT"


def test_drop_result_carries_only_reason_and_msgid_hash(mailer: Mailer) -> None:
    from khugjil.ingest.checks import Dropped

    assert {f.name for f in dataclasses.fields(Dropped)} == {"reason", "msgid_sha256"}
    r = evaluate(build_message(), mailer)
    assert len(r.msgid_sha256) == 64
    assert "fake-1@bank.example" not in repr(r)


# ---------------------------------------------------------------- a + logging (database)


@pytest.mark.db
async def test_a_unknown_recipient_rejected_nothing_stored(world, mailer: Mailer) -> None:  # type: ignore[no-untyped-def]
    before = await world.count("ingest_event"), await world.count("transaction")
    outcome = await world.ingest(mailer.bank_mail(), rcpt="nosuchaddress@in.khugjil.test")
    assert str(outcome.reason) == "RCPT_UNKNOWN"
    assert (await world.count("ingest_event"), await world.count("transaction")) == before


@pytest.mark.db
async def test_drop_logs_only_allowed_fields(world, mailer: Mailer, log_capture) -> None:  # type: ignore[no-untyped-def]
    outcome = await world.ingest(mailer.bank_mail(from_addr="evil@attacker.example"))
    assert str(outcome.reason) == "FROM_NOT_ALLOWED"
    rows = await world.rows("ingest_event")
    assert set(rows[-1]) == {"id", "tenant_id", "reason_code", "msgid_sha256", "received_at"}
    drop_lines = [json.loads(x) for x in log_capture.getvalue().splitlines() if "ingest." in x]
    assert drop_lines
    for line in drop_lines:
        assert set(line) <= {
            "event",
            "level",
            "timestamp",
            "tenant_id",
            "reason_code",
            "msgid_sha256",
            "transport",
            "transaction_id",
        }
    assert PAYER_ACCOUNT not in log_capture.getvalue()
