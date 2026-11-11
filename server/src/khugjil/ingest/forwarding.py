"""Gmail forwarding-confirmation mail (between checks a and b).

Gmail's confirmation must carry Google's DKIM signature (d=google.com) first. Then the numeric
code is taken from the subject ("(#123456789) …", in any language) or from a 9-digit number in the
text. Current Gmail confirmations carry no code, only a confirmation link: then the first
``https://mail.google.com/…`` or ``https://mail-settings.google.com/…`` link is kept so the OWNER
can open it from the email page. The service itself never opens it (no link following), and no
other link or text leaves this module. A message from
Gmail's forwarding sender that cannot be used is dropped with its own reason code, so the owner
can see on the email page that the confirmation arrived but was not recognised.
"""

from __future__ import annotations

import re
from email.message import EmailMessage, Message
from email.utils import getaddresses

from khugjil.ingest.dkim_arc import DnsFunc, dkim_pass
from khugjil.ingest.reasons import Reason
from khugjil.ingest.sanitize import clean_text
from khugjil.logs import get_logger

FORWARDING_SENDER = "forwarding-noreply@google.com"
_SUBJECT_CODE = re.compile(r"\(#(?P<code>\d{6,12})\)")
_BODY_CODE = re.compile(r"(?<!\d)(?P<code>\d{9})(?!\d)")
_TAG = re.compile(r"<[^>]*>")
# Google's own confirmation endpoints only; anything else is never kept.
CONFIRM_LINK = re.compile(
    r"https://mail(?:-settings)?\.google\.com/[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]{1,1900}"
)
MAX_BODY_CHARS = 20_000


def sender_address(msg: EmailMessage) -> str | None:
    values = msg.get_all("From") or []
    if len(values) != 1:
        return None
    addrs = [a for _, a in getaddresses([str(values[0])]) if a]
    if len(addrs) != 1:
        return None
    return addrs[0].strip().lower()


def _body_text(msg: EmailMessage) -> str:
    """The text parts (HTML with tags removed), capped; used only to find the digits."""
    out: list[str] = []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_maintype() != "text":
            continue
        try:
            text = str(part.get_content())
        except (LookupError, ValueError):
            continue
        if part.get_content_subtype() == "html":
            text = _TAG.sub(" ", text)
        out.append(text)
        if sum(len(t) for t in out) > MAX_BODY_CHARS:
            break
    return " ".join(out)[:MAX_BODY_CHARS]


def confirmation_code(msg: EmailMessage, raw: bytes, dnsfunc: DnsFunc) -> str | Reason | None:
    """None: not from Gmail's forwarding sender (the normal checks continue). A Reason: from that
    sender, but unusable. Otherwise the confirmation code."""
    if sender_address(msg) != FORWARDING_SENDER:
        return None
    if len(msg.get_all("Subject") or []) != 1:
        return Reason.FWD_CONFIRM_UNRECOGNIZED
    if not dkim_pass(raw, ("google.com",), dnsfunc):
        return Reason.FWD_CONFIRM_AUTH_FAIL
    subject = clean_text(str(msg["Subject"]))
    body = _body_text(msg)
    m = _SUBJECT_CODE.search(subject) or _BODY_CODE.search(body)
    if m is None:
        link = CONFIRM_LINK.search(body)
        if link is not None:
            return link.group(0)
        get_logger().info(
            "ingest.fwd_confirm_shape", fwd_shape=shape(msg), fwd_digits=digit_runs(subject, body)
        )
        return Reason.FWD_CONFIRM_UNRECOGNIZED
    return m.group("code")


def shape(msg: Message) -> str:
    """MIME structure only, e.g. ``multipart/alternative[text/plain,text/html]``."""
    if not msg.is_multipart():
        return msg.get_content_type()
    parts = msg.get_payload()
    if not isinstance(parts, list):
        return f"{msg.get_content_type()}[?]"
    inner = ",".join(shape(p) if isinstance(p, Message) else "?" for p in parts[:10])
    return f"{msg.get_content_type()}[{inner}]"


def digit_runs(subject: str, body: str) -> str:
    """Lengths of digit runs, e.g. ``subject:9 hash:1 body:9,4`` (no digits themselves)."""
    s_runs = ",".join(str(len(r)) for r in re.findall(r"\d+", subject)[:10]) or "-"
    b_runs = ",".join(str(len(r)) for r in re.findall(r"\d+", body)[:20]) or "-"
    return f"subject:{s_runs} hash:{int('#' in subject)} body:{b_runs} chars:{len(body)}"
