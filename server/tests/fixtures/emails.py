"""Builder for FAKE Khan-Bank-style notification emails (SPEC §4 sample layout).

Nothing here is real: domains end in .example, account numbers are 1111111111/2222222222 and
DKIM/ARC keys are generated at runtime and published through a stub DNS function.
"""

from __future__ import annotations

import base64
import html
from dataclasses import dataclass, field, replace
from email.message import EmailMessage
from email.policy import SMTP

import dkim
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.conftest import FAKE_BANK_DOMAIN, FAKE_BANK_SENDER, FAKE_SUBJECT

TENANT_ACCOUNT = "2222222222"
TENANT_ACCOUNT_2 = "3333333333"
PAYER_ACCOUNT = "1111111111"
PAYER_NAME = "B******H TESTPAYERMARKER"
SELLER_NAME = "М*****Т TESTSELLERMARKER"
BALANCE_MARKER = "Үлдэгдэл/Balance: 9,876,543.21 MNT"


@dataclass(frozen=True)
class FakeTxn:
    date: str = "2026.10.15 08:37:22"
    journal: str = "1234567"
    system_date: str = "2026.10.15 08:37:22"
    from_account: str = PAYER_ACCOUNT
    payer_name: str = PAYER_NAME
    from_amount: str = "2,800.00"
    from_currency: str = "MNT"
    amount_words: str = "/ Хоёр мянга найман зуун төгрөг /"
    to_label: str = "Кт/To"
    to_amount: str = "2,800.00"
    to_currency: str = "MNT"
    to_account: str = TENANT_ACCOUNT
    seller_name: str = SELLER_NAME
    description: str = "Худалдан авалт"
    footer: tuple[str, ...] = (BALANCE_MARKER, "Энэ имэйл автоматаар илгээгдсэн.")
    preamble: tuple[str, ...] = ()
    extra_rows_after_journal: tuple[tuple[str, ...], ...] = ()

    def with_(self, **kw: object) -> FakeTxn:
        return replace(self, **kw)  # type: ignore[arg-type]


def rows(t: FakeTxn) -> list[list[str]]:
    out: list[list[str]] = [[p] for p in t.preamble]
    out += [
        [f"Огноо/Date:{t.date}"],
        ["Шилжүүлгийн мэдээлэл/Transaction information"],
        [f"Журналын/Journal No:{t.journal}"],
        *[list(r) for r in t.extra_rows_after_journal],
        [f"Системийн огноо/System Date:{t.system_date}"],
        [
            "Дт/From",
            "IBAN/Дансны/Картын дугаар IBAN/Account/Card number",
            "Нэр/Name",
            "Дүн/Amount",
            "Ханш/Rate",
        ],
        [t.from_account, t.payer_name, t.from_amount, t.from_currency, "1.00"],
        [t.amount_words],
        [
            t.to_label,
            "Банкны дугаар/Branch No",
            "Банкны нэр/Bank Name",
            t.to_amount,
            t.to_currency,
            "1.00",
        ],
        ["05", "Khan Bank - Хаан банк"],
        ["IBAN/Дансны/Картын дугаар IBAN/Account/Card Number", "Нэр/Name"],
        [t.to_account, t.seller_name],
        ["Гүйлгээний утга/Transaction description:"],
        [t.description],
        ["Харилцагч танд баярлалаа./Thank you to our customers"],
    ]
    out += [[f] for f in t.footer]
    return out


def html_body(t: FakeTxn, *, links: bool = False) -> str:
    trs = []
    for r in rows(t):
        tds = "".join(f"<td>{html.escape(c)}</td>" for c in r)
        trs.append(f"<tr>{tds}</tr>")
    extra = ""
    if links:
        extra = (
            '<p><a href="https://tracker.example/click?id=1">Дэлгэрэнгүй</a></p>'
            '<img src="https://tracker.example/pixel.gif" alt="">'
        )
    return (
        "<html><head><style>td{padding:2px}</style><script>var x=1;</script></head><body>"
        f"<table>{''.join(trs)}</table>{extra}</body></html>"
    )


def plain_body(t: FakeTxn) -> str:
    return "\r\n".join("  ".join(r) for r in rows(t)) + "\r\n"


def build_message(
    t: FakeTxn | None = None,
    *,
    subject: str = FAKE_SUBJECT,
    from_addr: str = FAKE_BANK_SENDER,
    to_addr: str = "shop.owner@gmail.example",
    message_id: str = "<fake-1@bank.example>",
    fmt: str = "html",
    links: bool = False,
    attachment: bool = False,
    extra_headers: tuple[tuple[str, str], ...] = (),
    body_override: str | None = None,
) -> bytes:
    t = t or FakeTxn()
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = "Thu, 15 Oct 2026 08:37:30 +0800"
    msg["Message-ID"] = message_id
    for name, value in extra_headers:
        msg[name] = value
    if body_override is not None:
        msg.set_content(body_override, subtype="html", charset="utf-8", cte="base64")
    elif fmt == "html":
        msg.set_content(html_body(t, links=links), subtype="html", charset="utf-8", cte="base64")
    elif fmt == "plain":
        msg.set_content(plain_body(t), charset="utf-8", cte="base64")
    else:  # both
        msg.set_content(plain_body(t), charset="utf-8", cte="base64")
        msg.add_alternative(html_body(t), subtype="html", charset="utf-8", cte="base64")
    if attachment:
        msg.add_attachment(
            b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="statement.pdf"
        )
    return msg.as_bytes(policy=SMTP)


# ---------------------------------------------------------------- Golomt "Easy Info" (golomt_v1)

GOLOMT_ACCOUNT_MASKED = "222*****22"  # TENANT_ACCOUNT 2222222222 as Golomt prints it


@dataclass(frozen=True)
class FakeGolomt:
    """Fake Golomt Easy Info notification, after the layout the shop owner described."""

    kind: str = "ОРЛОГЫН ГҮЙЛГЭЭ"
    amount: str = "+2,000.00 MNT"
    date: str = "2026-10-15"
    account: str = GOLOMT_ACCOUNT_MASKED
    description: str = "Худалдан авалт"
    balance: str = "Үлдэгдэл: *******"
    preamble: tuple[str, ...] = ()
    footer: tuple[str, ...] = ()
    split_labels: bool = True  # the amount label on its own line, like the sample

    def with_(self, **kw: object) -> FakeGolomt:
        return replace(self, **kw)  # type: ignore[arg-type]


def golomt_rows(g: FakeGolomt) -> list[list[str]]:
    out: list[list[str]] = [[p] for p in g.preamble] + [[g.kind]]
    if g.split_labels:
        out += [["Гүйлгээний дүн:"], [g.amount]]
    else:
        out += [["Гүйлгээний дүн:", g.amount]]
    out += [
        [f"Гүйлгээний огноо:{g.date}"],
        [f"Дансны дугаар: {g.account}"],
        [f"Гүйлгээний утга: {g.description}"],
        [g.balance],
    ]
    return out + [[f] for f in g.footer]


def build_golomt_message(
    g: FakeGolomt | None = None,
    *,
    fmt: str = "plain",
    subject: str = FAKE_SUBJECT,
    message_id: str = "<fake-golomt-1@bank.example>",
    date: str = "Thu, 15 Oct 2026 14:05:09 +0800",
    logo: bool = False,
    pdf: bool = False,
    octet_logo: str | None = None,
) -> bytes:
    g = g or FakeGolomt()
    msg = EmailMessage()
    msg["From"] = FAKE_BANK_SENDER
    msg["To"] = "shop.owner@gmail.example"
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = message_id
    if fmt == "html":
        trs = "".join(
            "<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in r) + "</tr>"
            for r in golomt_rows(g)
        )
        msg.set_content(f"<html><body><table>{trs}</table></body></html>", subtype="html",
                        charset="utf-8", cte="base64")  # fmt: skip
    else:
        text = "\r\n".join("  ".join(r) for r in golomt_rows(g)) + "\r\n"
        msg.set_content(text, charset="utf-8", cte="base64")
    if logo:  # like Gmail's "Attachments": the bank's logo image
        msg.add_related(b"\x89PNG\r\n\x1a\nfake", maintype="image", subtype="png", cid="<logo>")
    if octet_logo is not None:  # like Golomt's real email: the logo as application/octet-stream
        msg.add_attachment(b"\x89PNG\r\n\x1a\nfake", maintype="application",
                           subtype="octet-stream", filename=octet_logo)  # fmt: skip
    if pdf:
        msg.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf",
                           filename="statement.pdf")  # fmt: skip
    return msg.as_bytes(policy=SMTP)


def _keypair() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    pub = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return priv, b"v=DKIM1; k=rsa; p=" + base64.b64encode(pub)


SIGNED_HEADERS = [b"from", b"to", b"subject", b"date", b"message-id"]


@dataclass
class Mailer:
    """Signs fake mail like the bank and forwards it like Gmail, with test keys only."""

    keys: dict[str, tuple[bytes, bytes]] = field(default_factory=dict)
    lookups: list[bytes] = field(default_factory=list)

    def __post_init__(self) -> None:
        for name in ("bank", "google", "attacker"):
            self.keys[name] = _keypair()
        self.records = {
            f"s1._domainkey.{FAKE_BANK_DOMAIN}.".encode(): self.keys["bank"][1],
            b"g1._domainkey.google.com.": self.keys["google"][1],
            b"x1._domainkey.attacker.example.": self.keys["attacker"][1],
            # Attacker-controlled record published under the attacker's own domain only.
        }

    def dns(self, name: bytes | str, timeout: int = 5) -> bytes | None:
        key = name if isinstance(name, bytes) else name.encode()
        self.lookups.append(key)
        return self.records.get(key)

    def dkim_sign(
        self,
        raw: bytes,
        *,
        domain: str = FAKE_BANK_DOMAIN,
        selector: str = "s1",
        key: str = "bank",
        length: bool = False,
        headers: list[bytes] | None = None,
    ) -> bytes:
        sig = dkim.sign(
            raw,
            selector.encode(),
            domain.encode(),
            self.keys[key][0],
            include_headers=headers or SIGNED_HEADERS,
            length=length,
        )
        return bytes(sig) + raw

    def bank_mail(self, t: FakeTxn | None = None, **kw: object) -> bytes:
        return self.dkim_sign(build_message(t, **kw))  # type: ignore[arg-type]

    def golomt_mail(self, g: FakeGolomt | None = None, **kw: object) -> bytes:
        return self.dkim_sign(build_golomt_message(g, **kw))  # type: ignore[arg-type]

    def gmail_forward(
        self,
        raw: bytes,
        *,
        aar: str = f"dkim=pass header.i=@{FAKE_BANK_DOMAIN} header.s=s1 header.b=abc",
        sealer: str = "google.com",
        selector: str = "g1",
        key: str = "google",
        srv_id: str = "mx.google.com",
    ) -> bytes:
        ar = f"Authentication-Results: {srv_id}; {aar}\r\n".encode()
        arc = dkim.arc_sign(
            ar + raw, selector.encode(), sealer.encode(), self.keys[key][0], srv_id.encode()
        )
        return b"".join(arc) + ar + b"X-Forwarded-To: fake@in.khugjil.test\r\n" + raw


def break_dkim(raw: bytes) -> bytes:
    """Change a signed header after signing, the way a forwarder can break DKIM."""
    out = raw.replace(b"To: shop.owner@gmail.example", b"To: shop.owner+fwd@gmail.example", 1)
    assert out != raw
    return out


def append_to_body(raw: bytes, extra: bytes) -> bytes:
    return raw.rstrip(b"\r\n") + b"\r\n" + extra + b"\r\n"


def add_header_after_signing(raw: bytes, name: str, value: str) -> bytes:
    return f"{name}: {value}\r\n".encode() + raw


def forwarding_confirmation(code: str = "123456789") -> bytes:
    msg = EmailMessage()
    msg["From"] = "Gmail Team <forwarding-noreply@google.com>"
    msg["To"] = "fake@in.khugjil.test"
    msg["Subject"] = (
        f"(#{code}) Gmail Forwarding Confirmation - Receive Mail from shop.owner@gmail.example"
    )
    msg["Date"] = "Thu, 15 Oct 2026 08:40:00 +0800"
    msg["Message-ID"] = "<fwd-1@google.com>"
    msg.set_content(
        "shop.owner@gmail.example has requested to automatically forward mail to your email\r\n"
        "address fake@in.khugjil.test.\r\n"
        f"Confirmation code: {code}\r\n"
        "To allow shop.owner@gmail.example to automatically forward mail to your address,\r\n"
        "please click the link below to confirm the request:\r\n"
        "https://mail-settings.google.com/mail/vf-fake\r\n",
        charset="utf-8",
    )
    return msg.as_bytes(policy=SMTP)
