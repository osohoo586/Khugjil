"""SPEC §3.3: deny-list on subject and on body EXCLUDING the payer-written description."""

from __future__ import annotations

import pytest

from tests.fixtures.emails import FakeTxn, Mailer
from tests.invariants.helpers import evaluate

pytestmark = pytest.mark.invariant(3)

DENY_WORDS = [
    "OTP",
    "нэг удаагийн",
    "код",
    "нууц үг",
    "password",
    "verification",
    "баталгаажуулах",
    "PIN",
    "нэвтрэх",
    "login",
]


def reason(r: object) -> str:
    from khugjil.ingest.checks import Accepted

    return "ACCEPTED" if isinstance(r, Accepted) else str(r.reason)  # type: ignore[attr-defined]


@pytest.mark.parametrize("word", DENY_WORDS)
def test_each_word_is_denied(word: str) -> None:
    from khugjil.ingest.denylist import contains_denied

    assert contains_denied(f"Таны {word} энд байна")
    assert contains_denied(f"Таны {word.upper()} энд байна")


@pytest.mark.parametrize("text", ["кодыг оруулна уу", "нууц үгээ хэнд ч бүү хэл", "нэвтрэхэд"])
def test_inflected_mongolian_forms_are_denied(text: str) -> None:
    from khugjil.ingest.denylist import contains_denied

    assert contains_denied(text)


@pytest.mark.parametrize("text", ["PINO***A TEST", "Opinion", "Худалдан авалт", "LOGINOV"])
def test_no_false_positives_inside_latin_words(text: str) -> None:
    from khugjil.ingest.denylist import contains_denied

    assert not contains_denied(text)


def test_otp_subject_is_denied_before_subject_check(mailer: Mailer) -> None:
    raw = mailer.bank_mail(subject="Таны OTP код: 123456")
    assert reason(evaluate(raw, mailer)) == "DENYLIST"


def test_deny_word_in_template_part_is_denied(mailer: Mailer) -> None:
    t = FakeTxn(footer=("Нэг удаагийн код: 123456",))
    assert reason(evaluate(mailer.bank_mail(t), mailer)) == "DENYLIST"


def test_deny_word_in_preamble_is_denied(mailer: Mailer) -> None:
    t = FakeTxn(preamble=("Your PIN is 1234",))
    assert reason(evaluate(mailer.bank_mail(t), mailer)) == "DENYLIST"


@pytest.mark.parametrize("desc", ["Захиалгын код 4411", "PIN 1234 OTP", "нууц үг биш"])
def test_deny_word_inside_description_is_allowed(mailer: Mailer, desc: str) -> None:
    r = evaluate(mailer.bank_mail(FakeTxn(description=desc)), mailer)
    assert reason(r) == "ACCEPTED"
    assert r.description == desc


def test_masked_payer_name_does_not_trigger(mailer: Mailer) -> None:
    r = evaluate(mailer.bank_mail(FakeTxn(payer_name="P******O PINOTEST")), mailer)
    assert reason(r) == "ACCEPTED"
