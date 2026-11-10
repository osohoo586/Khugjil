"""Gmail forwarding-confirmation mail (between checks a and b).

Only the numeric confirmation code is extracted, from the DKIM-signed subject. No link is read,
stored or followed. Anything that does not match exactly falls through to check b and is dropped.
"""

from __future__ import annotations

import re
from email.message import EmailMessage
from email.utils import getaddresses

from khugjil.ingest.dkim_arc import DnsFunc, dkim_pass
from khugjil.ingest.sanitize import clean_text

FORWARDING_SENDER = "forwarding-noreply@google.com"
_SUBJECT = re.compile(
    r"\(#(?P<code>\d{6,12})\) Gmail Forwarding Confirmation - Receive Mail from \S{3,254}"
)


def sender_address(msg: EmailMessage) -> str | None:
    values = msg.get_all("From") or []
    if len(values) != 1:
        return None
    addrs = [a for _, a in getaddresses([str(values[0])]) if a]
    if len(addrs) != 1:
        return None
    return addrs[0].strip().lower()


def confirmation_code(msg: EmailMessage, raw: bytes, dnsfunc: DnsFunc) -> str | None:
    if sender_address(msg) != FORWARDING_SENDER:
        return None
    if len(msg.get_all("Subject") or []) != 1:
        return None
    m = _SUBJECT.fullmatch(clean_text(str(msg["Subject"])))
    if m is None:
        return None
    if msg.is_multipart() or msg.get_content_type() != "text/plain":
        return None
    if not dkim_pass(raw, ("google.com",), dnsfunc):
        return None
    return m.group("code")
