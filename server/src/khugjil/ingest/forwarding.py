"""Gmail forwarding-confirmation mail (between checks a and b).

Only the numeric confirmation code is extracted, from the DKIM-signed subject ("(#123456789) …",
in whatever language the Gmail account uses). The body is never read, so a plain-text or a
text+HTML message both work; no link is read, stored or followed. A message from Gmail's
forwarding sender that cannot be used is dropped with its own reason code, so the owner can see
on the email page that the confirmation arrived but was not recognised.
"""

from __future__ import annotations

import re
from email.message import EmailMessage
from email.utils import getaddresses

from khugjil.ingest.dkim_arc import DnsFunc, dkim_pass
from khugjil.ingest.reasons import Reason
from khugjil.ingest.sanitize import clean_text

FORWARDING_SENDER = "forwarding-noreply@google.com"
_SUBJECT = re.compile(r"\(#(?P<code>\d{6,12})\)\s")
_TEXT_TYPES = frozenset({"text/plain", "text/html"})


def sender_address(msg: EmailMessage) -> str | None:
    values = msg.get_all("From") or []
    if len(values) != 1:
        return None
    addrs = [a for _, a in getaddresses([str(values[0])]) if a]
    if len(addrs) != 1:
        return None
    return addrs[0].strip().lower()


def _text_only(msg: EmailMessage) -> bool:
    """Plain text, or text+HTML alternatives; nothing else, no attachments."""
    if not msg.is_multipart():
        return msg.get_content_type() in _TEXT_TYPES
    if msg.get_content_type() != "multipart/alternative":
        return False
    parts = msg.get_payload()
    return (
        isinstance(parts, list)
        and bool(parts)
        and all(
            isinstance(p, EmailMessage)
            and not p.is_multipart()
            and p.get_content_type() in _TEXT_TYPES
            and p.get_content_disposition() is None
            for p in parts
        )
    )


def confirmation_code(msg: EmailMessage, raw: bytes, dnsfunc: DnsFunc) -> str | Reason | None:
    """None: not from Gmail's forwarding sender (the normal checks continue). A Reason: from that
    sender, but unusable. Otherwise the confirmation code."""
    if sender_address(msg) != FORWARDING_SENDER:
        return None
    if len(msg.get_all("Subject") or []) != 1:
        return Reason.FWD_CONFIRM_UNRECOGNIZED
    m = _SUBJECT.match(clean_text(str(msg["Subject"])))
    if m is None or not _text_only(msg):
        return Reason.FWD_CONFIRM_UNRECOGNIZED
    if not dkim_pass(raw, ("google.com",), dnsfunc):
        return Reason.FWD_CONFIRM_AUTH_FAIL
    return m.group("code")
