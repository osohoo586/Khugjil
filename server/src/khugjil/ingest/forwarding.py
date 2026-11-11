"""Gmail forwarding-confirmation mail (between checks a and b).

Only the numeric confirmation code is extracted. Gmail's confirmation must carry Google's DKIM
signature (d=google.com) first; then the code is taken from the subject ("(#123456789) …", in
whatever language the Gmail account uses) or, failing that, from a 9-digit number in the text.
No link is read, stored or followed, and nothing but the digits leaves this module. A message from
Gmail's forwarding sender that cannot be used is dropped with its own reason code, so the owner
can see on the email page that the confirmation arrived but was not recognised.
"""

from __future__ import annotations

import re
from email.message import EmailMessage
from email.utils import getaddresses

from khugjil.ingest.dkim_arc import DnsFunc, dkim_pass
from khugjil.ingest.reasons import Reason
from khugjil.ingest.sanitize import clean_text

FORWARDING_SENDER = "forwarding-noreply@google.com"
_SUBJECT_CODE = re.compile(r"\(#(?P<code>\d{6,12})\)")
_BODY_CODE = re.compile(r"(?<!\d)(?P<code>\d{9})(?!\d)")
_TAG = re.compile(r"<[^>]*>")
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
    m = _SUBJECT_CODE.search(clean_text(str(msg["Subject"])))
    if m is None:
        m = _BODY_CODE.search(_body_text(msg))
    return m.group("code") if m else Reason.FWD_CONFIRM_UNRECOGNIZED
