"""SPEC §3.6: the description is untrusted payer input."""

from __future__ import annotations

import unicodedata
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.fixtures.emails import TENANT_ACCOUNT, FakeTxn, Mailer
from tests.invariants.helpers import SRC_DIR, evaluate, pending

pytestmark = pytest.mark.invariant(6)

BIDI_AND_INVISIBLE = [
    "​",
    "‌",
    "‍",
    "‎",
    "‏",
    "⁠",
    "﻿",
    "؜",
    "‪",
    "‫",
    "‬",
    "‭",
    "‮",
    "⁦",
    "⁧",
    "⁨",
    "⁩",
]


@pytest.mark.parametrize("ch", BIDI_AND_INVISIBLE)
def test_strips_bidi_and_zero_width(ch: str) -> None:
    from khugjil.ingest.sanitize import sanitize_description

    assert sanitize_description(f"Худалдан{ch} авалт") == "Худалдан авалт"


def test_strips_control_chars_and_collapses_whitespace() -> None:
    from khugjil.ingest.sanitize import sanitize_description

    assert sanitize_description("a\x00b\x07c\r\n\td   e\x7f") == "abc d e"


def test_truncates_to_200_without_dangling_combining_mark() -> None:
    from khugjil.ingest.sanitize import sanitize_description

    out = sanitize_description("а" * 199 + "ё" + "x" * 50)
    assert len(out) <= 200
    assert not unicodedata.combining(out[-1])
    assert len(sanitize_description("ж" * 10_000)) == 200


@given(st.text(max_size=2000))
def test_sanitizer_output_is_always_clean(text: str) -> None:
    from khugjil.ingest.sanitize import sanitize_description

    out = sanitize_description(text)
    assert len(out) <= 200
    assert out == out.strip()
    for ch in out:
        assert unicodedata.category(ch) not in {"Cc", "Cf", "Co", "Cs", "Cn"}, repr(ch)


def test_label_like_description_cannot_move_other_fields(mailer: Mailer) -> None:
    desc = "Дүн/Amount 9,999,999.00 MNT Кт/To 9999999999 Журналын/Journal No:1"
    r = evaluate(mailer.bank_mail(FakeTxn(description=desc)), mailer)
    assert r.amount == Decimal("2800.00")
    assert r.description == desc


def test_terminator_phrase_inside_description_is_kept(mailer: Mailer) -> None:
    desc = "Харилцагч танд баярлалаа гэж хэлээрэй"
    r = evaluate(mailer.bank_mail(FakeTxn(description=desc)), mailer)
    assert r.description == desc


@pytest.mark.parametrize(
    "desc",
    ["<script>alert(1)</script>", "<img src=x onerror=alert(1)>", "**bold** [x](http://e)"],
)
def test_markup_is_kept_as_literal_text(mailer: Mailer, desc: str) -> None:
    r = evaluate(mailer.bank_mail(FakeTxn(description=desc)), mailer)
    assert r.description == desc


def test_huge_description_is_truncated(mailer: Mailer) -> None:
    r = evaluate(mailer.bank_mail(FakeTxn(description="ж" * 5000)), mailer)
    assert len(r.description) == 200


def test_telegram_payload_is_plain_text() -> None:
    from khugjil.delivery.telegram import send_message_payload

    payload = send_message_payload(chat_id=1, text="<b>x</b> *y*")
    assert "parse_mode" not in payload
    assert payload["text"] == "<b>x</b> *y*"
    assert payload["link_preview_options"] == {"is_disabled": True}


@pending("admin panel")
def test_admin_templates_autoescape() -> None:
    from khugjil.admin.templating import env

    out = env.from_string("{{ d }}").render(d="<script>alert(1)</script>")
    assert "<script>" not in out


def test_no_safe_filter_or_markup_in_templates_or_code() -> None:
    bad = []
    for path in SRC_DIR.rglob("*"):
        if path.suffix not in {".html", ".jinja", ".py"}:
            continue
        text = path.read_text(encoding="utf-8")
        if path.suffix != ".py" and (
            "|safe" in text.replace(" ", "") or "autoescape false" in text
        ):
            bad.append(path.name)
        if path.suffix == ".py" and ("Markup(" in text or "autoescape=False" in text):
            bad.append(path.name)
    assert bad == []


def test_account_marker_not_in_fixture_description() -> None:
    assert TENANT_ACCOUNT not in FakeTxn().description
