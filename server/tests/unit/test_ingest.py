"""Unit tests for the ingest core beyond the SPEC §3 invariants (parser, MIME, DKIM/ARC edges)."""

from __future__ import annotations

import contextlib
import html
from decimal import Decimal
from email.message import EmailMessage
from email.policy import SMTP

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from khugjil.ingest import mime
from khugjil.ingest.checks import Accepted, Dropped, ForwardingCode
from khugjil.ingest.dkim_arc import _aar_records_pass, arc_pass, dkim_pass
from khugjil.ingest.parsers import khan_v1
from khugjil.ingest.reasons import Reason
from khugjil.ingest.sanitize import sanitize_description
from tests.fixtures.emails import (
    TENANT_ACCOUNT,
    FakeTxn,
    Mailer,
    build_message,
    forwarding_confirmation,
    plain_body,
    rows,
)
from tests.invariants.helpers import evaluate

# ---------------------------------------------------------------- forwarding confirmation


def _google_signed(mailer: Mailer, raw: bytes) -> bytes:
    return mailer.dkim_sign(raw, domain="google.com", selector="g1", key="google")


def test_forwarding_confirmation_extracts_code_only(mailer: Mailer) -> None:
    r = evaluate(_google_signed(mailer, forwarding_confirmation("987654321")), mailer)
    assert isinstance(r, ForwardingCode)
    assert r.code.reveal() == "987654321"
    assert "987654321" not in repr(r)
    assert "mail-settings" not in repr(r)


def test_forwarding_confirmation_requires_google_dkim(mailer: Mailer) -> None:
    r = evaluate(forwarding_confirmation(), mailer)
    assert isinstance(r, Dropped)
    assert r.reason == Reason.FROM_NOT_ALLOWED


def test_forwarding_confirmation_requires_exact_subject(mailer: Mailer) -> None:
    raw = forwarding_confirmation().replace(b"Gmail Forwarding Confirmation", b"Gmail Notice")
    r = evaluate(_google_signed(mailer, raw), mailer)
    assert isinstance(r, Dropped)
    assert r.reason == Reason.FROM_NOT_ALLOWED


# ---------------------------------------------------------------- parser


def test_parser_rejects_each_broken_section() -> None:
    good = rows(FakeTxn())
    assert khan_v1.parse(good).to_account.reveal() == TENANT_ACCOUNT
    for i in [*range(12), 13]:  # every template row; 12 is the description itself
        broken = [r[:] for r in good]
        broken[i] = ["ЭВДЭРСЭН МӨР"]
        with pytest.raises(khan_v1.TemplateMismatchError):
            khan_v1.parse(broken)


@pytest.mark.parametrize(
    "txn",
    [
        FakeTxn(date="2026.13.40 99:99:99"),
        FakeTxn(system_date="2026.02.30 08:00:00"),
        FakeTxn(from_amount="2800"),
        FakeTxn(to_currency="mnt"),
        FakeTxn(to_account="22-22"),
    ],
)
def test_parser_rejects_bad_values(txn: FakeTxn) -> None:
    with pytest.raises(khan_v1.TemplateMismatchError):
        khan_v1.parse(rows(txn))


def test_parser_handles_missing_names_and_amount_words() -> None:
    t = FakeTxn(payer_name="", seller_name="", amount_words="")
    parsed = khan_v1.parse(mime.plain_rows(plain_body(t)))
    assert parsed.to_amount == Decimal("2800.00")


def test_parser_preamble_limit() -> None:
    ok = FakeTxn(preamble=tuple(f"мөр {i}" for i in range(10)))
    khan_v1.parse(rows(ok))
    too_long = FakeTxn(preamble=tuple(f"мөр {i}" for i in range(11)))
    with pytest.raises(khan_v1.TemplateMismatchError):
        khan_v1.parse(rows(too_long))


def test_parser_inline_description_and_multirow() -> None:
    r = rows(FakeTxn())
    label = r.index(["Гүйлгээний утга/Transaction description:"])
    r[label] = ["Гүйлгээний утга/Transaction description: эхний хэсэг"]
    r.insert(label + 2, ["хоёр дахь", "хэсэг"])
    parsed = khan_v1.parse(r)
    assert parsed.description_raw == "эхний хэсэг Худалдан авалт хоёр дахь хэсэг"
    assert "эхний хэсэг" not in parsed.non_description_text


def test_parser_requires_terminator() -> None:
    r = rows(FakeTxn())
    r = r[: r.index(["Харилцагч танд баярлалаа./Thank you to our customers"])]
    with pytest.raises(khan_v1.TemplateMismatchError):
        khan_v1.parse(r)


def test_canonical_account() -> None:
    assert khan_v1.canonical_account(" 2222-2222 22 ") == "2222222222"
    assert khan_v1.canonical_account("mn12") == "MN12"


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(desc=st.text(max_size=400))
def test_fuzz_description_never_moves_fields(mailer: Mailer, desc: str) -> None:
    r = evaluate(mailer.bank_mail(FakeTxn(description=desc)), mailer)
    assert isinstance(r, Accepted), r
    assert r.amount == Decimal("2800.00")
    assert r.account_id.int == 1
    assert r.description == sanitize_description(html.unescape(html.escape(desc)))


@settings(max_examples=60, deadline=None)
@given(junk=st.lists(st.lists(st.text(max_size=40), max_size=6), max_size=40))
def test_fuzz_parser_only_raises_template_mismatch(junk: list[list[str]]) -> None:
    with contextlib.suppress(khan_v1.TemplateMismatchError):
        khan_v1.parse(junk)


@settings(max_examples=60, deadline=None)
@given(markup=st.text(max_size=2000))
def test_fuzz_html_normalizer_never_crashes(markup: str) -> None:
    for row in mime.html_rows(markup):
        assert all(cell == cell.strip() and cell for cell in row)


# ---------------------------------------------------------------- MIME


def test_html_normalizer_ignores_scripts_links_and_attributes() -> None:
    out = mime.html_rows(
        "<html><head><title>T</title><style>x{}</style></head><body>"
        '<p>нэг<br>хоёр</p><table><tr><td>a<br/>b</td><td><a href="https://e.example">c</a></td>'
        '</tr></table><img src="https://e.example/p.gif"/><script>evil()</script>сүүл</body></html>'
    )
    assert out == [["нэг"], ["хоёр"], ["a b", "c"], ["сүүл"]]


def test_html_normalizer_caps_nodes() -> None:
    with pytest.raises(mime.MimeError):
        mime.html_rows("<b>x</b>" * (mime.MAX_HTML_NODES + 1))


def _msg(**kw: object) -> EmailMessage:
    m = EmailMessage()
    m["From"] = "a@b.example"
    m["Subject"] = "x"
    for k, v in kw.items():
        m[k] = v
    return m


def test_inspect_rejects_non_text_parts() -> None:
    m = _msg()
    m.set_content("hello")
    m.add_related(b"\x89PNG", maintype="image", subtype="png", cid="<logo>")
    assert mime.inspect_bodies(m, 100) == Reason.ATTACHMENT


def test_inspect_rejects_deep_nesting() -> None:
    inner = _msg()
    inner.set_content("x")
    for _ in range(mime.MAX_MIME_DEPTH + 1):
        outer = EmailMessage()
        outer.make_mixed()
        outer.attach(inner)
        inner = outer
    assert mime.inspect_bodies(inner, 100) == Reason.MIME_INVALID


def test_inspect_rejects_unknown_charset_and_empty() -> None:
    raw = (
        b"From: a@b.example\r\nSubject: x\r\n"
        b"Content-Type: text/plain; charset=no-such-cs\r\n\r\nhi\r\n"
    )
    assert mime.inspect_bodies(mime.parse_message(raw), len(raw)) == Reason.MIME_INVALID
    empty = _msg()
    empty.make_mixed()
    assert mime.inspect_bodies(empty, 10) == Reason.MIME_INVALID


def test_inspect_rejects_message_rfc822_and_multipart_report() -> None:
    m = _msg()
    m["Content-Type"] = "multipart/report; boundary=x"
    raw = m.as_bytes(policy=SMTP) + b"--x\r\nContent-Type: text/plain\r\n\r\nhi\r\n--x--\r\n"
    assert mime.inspect_bodies(mime.parse_message(raw), len(raw)) == Reason.ATTACHMENT


def test_message_id_hash_is_stable() -> None:
    a = mime.parse_message(build_message())
    assert mime.message_id_hash(a) == mime.message_id_hash(mime.parse_message(build_message()))


# ---------------------------------------------------------------- DKIM / ARC helpers


def test_aar_parser_edges() -> None:
    domains = {"bank.example"}
    assert _aar_records_pass(
        "i=1; mx.google.com; dkim=pass (2048-bit) header.d=bank.example", domains
    )
    assert not _aar_records_pass("i=1; mx.google.com", domains)
    assert not _aar_records_pass("mx.google.com; dkim=pass header.d=bank.example; x", domains)
    assert not _aar_records_pass("i=1; ; dkim=pass header.d=bank.example", domains)
    assert not _aar_records_pass("i=1; mx.google.com; dkim=pass header.s=s1", domains)


def test_dkim_and_arc_on_garbage(mailer: Mailer) -> None:
    assert not dkim_pass(b"not an email", ["bank.example"], mailer.dns)
    assert not arc_pass(b"not an email", ["bank.example"], mailer.dns)
    bad_sig = b"DKIM-Signature: v=1; d=bank.example; =broken\r\n" + build_message()
    assert not dkim_pass(bad_sig, ["bank.example"], mailer.dns)
    garbage_key = (
        b"DKIM-Signature: v=1; a=rsa-sha256; d=bank.example; s=s1; "
        b"h=from:subject:date; bh=x; b=y\r\n"
    )
    assert not dkim_pass(garbage_key + build_message(), ["bank.example"], mailer.dns)


def test_unparseable_message_is_mime_invalid(mailer: Mailer) -> None:
    r = evaluate(b"\x00\xff" * 10, mailer)
    assert isinstance(r, Dropped)


# ---------------------------------------------------------------- coverage of edge branches


def test_truncation_drops_trailing_combining_mark() -> None:
    out = sanitize_description("q" * 199 + "́" + "zz")
    assert out == "q" * 199


def test_html_normalizer_structural_edges() -> None:
    out = mime.html_rows(
        "<td>orphan cell</td><div>блок</div><style/>текст<table><tr><td>a</td></table>"
        "<p>x</p></div>"
    )
    assert out == [["orphan cell"], ["блок"], ["текст"], ["a"], ["x"]]


def test_forwarding_rejects_variants(mailer: Mailer) -> None:
    from khugjil.ingest.forwarding import confirmation_code

    def check(raw: bytes) -> str | None:
        signed = _google_signed(mailer, raw)
        return confirmation_code(mime.parse_message(signed), signed, mailer.dns)

    base = forwarding_confirmation()
    assert check(base) == "123456789"
    assert check(b"From: x@google.com\r\n" + base) is None
    assert check(b"Subject: again\r\n" + base) is None
    multi = base.replace(b"Content-Type: text/plain", b"Content-Type: multipart/mixed; boundary=b")
    assert check(multi) is None


def test_message_id_hash_survives_malformed_header() -> None:
    msg = mime.parse_message(b"Message-ID: <\x00broken\r\n\r\nx")
    assert len(mime.message_id_hash(msg)) == 64


def test_text_between_cells_is_not_dropped() -> None:
    out = mime.html_rows("<table><tr><td>a</td>OTP 1234<td>b</td></tr></table>")
    assert ["OTP 1234"] in out


def test_inspect_rejects_non_list_multipart_payload() -> None:
    raw = (
        b"From: a@b.example\r\nSubject: x\r\nContent-Type: multipart/mixed\r\n\r\n"
        b"no boundary at all\r\n"
    )
    assert mime.inspect_bodies(mime.parse_message(raw), len(raw)) in {
        Reason.MIME_INVALID,
        Reason.ATTACHMENT,
    }


def test_inspect_decoded_size_limit() -> None:
    m = _msg()
    m.set_content("ж" * (mime.MAX_MESSAGE_BYTES // 2 + 10))
    assert mime.inspect_bodies(m, 100) == Reason.TOO_LARGE


def test_html_tags_inside_skipped_regions_and_whitespace_between_cells() -> None:
    out = mime.html_rows("<style><b>x</b></style><table><tr><td>a</td> <td>b</td></tr></table>")
    assert out == [["a", "b"]]


def test_first_html_part_wins() -> None:
    m = _msg()
    m.set_content("<p>first</p>", subtype="html")
    m.add_alternative("<p>second</p>", subtype="html")
    bodies = mime.inspect_bodies(m, 100)
    assert isinstance(bodies, mime.Bodies)
    assert bodies.html is not None
    assert "first" in bodies.html
