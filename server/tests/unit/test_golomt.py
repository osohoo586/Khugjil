"""golomt_v1 edge cases: layout variants, time handling, masked matching, signature ids."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from khugjil.ingest.checks import AccountMatch, masked_match
from khugjil.ingest.dkim_arc import bank_signature_id
from khugjil.ingest.parsers import golomt_v1
from tests.fixtures.emails import TENANT_ACCOUNT, FakeGolomt, Mailer, build_golomt_message
from tests.invariants.test_inv02_golomt import evaluate

UB = ZoneInfo("Asia/Ulaanbaatar")


def rows(*lines: str) -> list[list[str]]:
    return [[line] for line in lines]


BASE = (
    "ОРЛОГЫН ГҮЙЛГЭЭ",
    "Гүйлгээний дүн:",
    "+15,000.00 MNT",
    "Гүйлгээний огноо:2026-10-15",
    "Дансны дугаар: 222*****22",
    "Гүйлгээний утга: Захиалга",
    "Үлдэгдэл: *******",
)


def test_parse_base_layout() -> None:
    p = golomt_v1.parse(rows(*BASE))
    assert (p.amount, p.currency, p.date, p.time) == (15000, "MNT", "2026-10-15", None)
    assert p.account_masked.reveal() == "222*****22"
    assert p.description_raw == "Захиалга"
    assert "Захиалга" not in p.non_description_text


def test_description_on_following_rows() -> None:
    lines = list(BASE)
    lines[5:6] = ["Гүйлгээний утга:", "Урт тайлбар", "хоёр мөр"]
    p = golomt_v1.parse(rows(*lines))
    assert p.description_raw == "Урт тайлбар хоёр мөр"
    assert "Урт тайлбар" not in p.non_description_text


@pytest.mark.parametrize(
    "lines",
    [
        BASE[:3],  # ends early
        (BASE[0], "Дүн:", *BASE[2:]),  # unknown label
        (*BASE[:5], "Утга: Захиалга", BASE[6]),  # description label changed
        (*BASE[:6],),  # no balance row
    ],
)
def test_layout_changes_are_template_mismatches(lines: tuple[str, ...]) -> None:
    with pytest.raises(golomt_v1.TemplateMismatchError):
        golomt_v1.parse(rows(*lines))


@pytest.mark.parametrize(
    ("date", "expected"),
    [
        ("2026-10-15 09:12", datetime(2026, 10, 15, 9, 12, tzinfo=UB)),
        ("2026-10-15 09:12:30", datetime(2026, 10, 15, 9, 12, 30, tzinfo=UB)),
    ],
)
def test_printed_time_wins_over_the_date_header(mailer: Mailer, date: str, expected: Any) -> None:
    assert evaluate(mailer.golomt_mail(FakeGolomt(date=date)), mailer).txn_at == expected


@pytest.mark.parametrize("date", ["2026-13-45", "2026-10-15 25:99"])
def test_impossible_dates_are_template_mismatches(mailer: Mailer, date: str) -> None:
    raw = mailer.golomt_mail(FakeGolomt(date=date))
    assert str(evaluate(raw, mailer).reason) == "TEMPLATE_MISMATCH"


def test_unreadable_date_header_falls_back_to_start_of_day(mailer: Mailer) -> None:
    raw = mailer.golomt_mail(date="not a date")
    assert evaluate(raw, mailer).txn_at == datetime(2026, 10, 15, tzinfo=UB)


def test_arc_pass_without_any_bank_signature_is_refused(mailer: Mailer) -> None:
    """Gmail's ARC may vouch for a bank DKIM pass, but without the bank's own signature header
    there is nothing stable to deduplicate on, so the email is not accepted."""
    raw = mailer.gmail_forward(build_golomt_message())
    assert str(evaluate(raw, mailer).reason) == "AUTH_FAIL"


def test_masked_match_rules() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    accounts = {TENANT_ACCOUNT: AccountMatch(a, True), "3333333333": AccountMatch(b, False)}
    hit = masked_match("222*****22", accounts)
    assert hit is not None
    assert hit.account_id == a
    assert hit.canonical is not None
    assert hit.canonical.reveal() == TENANT_ACCOUNT
    inactive = masked_match("333*****33", accounts)
    assert inactive is not None
    assert (inactive.account_id, inactive.active) == (b, False)
    for bad in ("22*******2", "2**2", "ab2*****22", "222*****2"):
        assert masked_match(bad, accounts) is None, bad


def test_bank_signature_id(mailer: Mailer) -> None:
    signed = mailer.golomt_mail()
    sid = bank_signature_id(signed, ["bank.example"])
    assert sid is not None
    assert len(sid) == 64
    assert bank_signature_id(signed, ["other.example"]) is None
    assert bank_signature_id(build_golomt_message(), ["bank.example"]) is None
    broken = b"DKIM-Signature: v=1; this is not a tag list\r\n" + build_golomt_message()
    assert bank_signature_id(broken, ["bank.example"]) is None
    assert bank_signature_id(b"\x00", ["bank.example"]) is None


def test_failed_bank_email_logs_only_its_signing_domains(mailer: Mailer, log_capture: Any) -> None:
    """A new bank's DKIM domain is found from its first email: only d= values are logged."""
    import json

    raw = mailer.dkim_sign(
        build_golomt_message(FakeGolomt(description="MARKER")),
        domain="attacker.example",
        selector="x1",
        key="attacker",
    )
    assert str(evaluate(raw, mailer).reason) == "AUTH_FAIL"
    out = log_capture.getvalue()
    assert "MARKER" not in out
    lines = [json.loads(x) for x in out.splitlines() if "ingest.auth_domains" in x]
    assert [x["dkim_domains"] for x in lines] == ["attacker.example"]
    assert evaluate(build_golomt_message(), mailer).reason.value == "AUTH_FAIL"
    assert '"dkim_domains": "-"' in log_capture.getvalue()


def test_signature_domains_edge_cases(mailer: Mailer) -> None:
    from khugjil.ingest.dkim_arc import signature_domains

    signed = mailer.golomt_mail()
    assert signature_domains(signed) == ["bank.example"]
    assert signature_domains(build_golomt_message()) == []
    assert signature_domains(b"\x00") == []
    broken = b"DKIM-Signature: v=1; this is not a tag list\r\n" + signed
    assert signature_domains(broken) == ["bank.example"]
    odd = b"DKIM-Signature: v=1; d=bad_domain!; s=x; b=a\r\n" + signed
    assert signature_domains(odd) == ["bank.example"]
    many = b"".join(b"DKIM-Signature: v=1; d=d%d.example; b=a\r\n" % i for i in range(8)) + signed
    assert signature_domains(many, limit=3) == ["d0.example", "d1.example", "d2.example"]
