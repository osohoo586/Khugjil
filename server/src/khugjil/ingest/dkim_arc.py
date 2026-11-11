"""Check c: DKIM for the bank domain, or a Gmail ARC chain that recorded a bank DKIM pass.

Hardening beyond plain DKIM verification:
* signatures with an ``l=`` (body length) tag are ignored, since they let anyone append content;
* the signature must cover From, Subject and Date (duplicates are rejected earlier, in check b);
* ARC is trusted only for the highest instance, only if sealed by google.com, and only if its
  ARC-Authentication-Results (authserv-id mx.google.com) records ``dkim=pass`` for exactly a
  configured bank domain.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable

import dkim
from dkim.util import InvalidTagValueList, parse_tag_value

DnsFunc = Callable[..., bytes | str | None]

REQUIRED_SIGNED_HEADERS = frozenset({b"from", b"subject", b"date"})
ARC_SEALER = b"google.com"
ARC_AUTHSERV = "mx.google.com"
_COMMENT = re.compile(r"\([^()]*\)")


def dkim_pass(raw: bytes, domains: Iterable[str], dnsfunc: DnsFunc) -> bool:
    wanted = {d.lower().encode() for d in domains}
    try:
        verifier = dkim.DKIM(raw, minkey=1024)
    except Exception:  # unparsable message
        return False
    signatures = [v for k, v in verifier.headers if k.lower() == b"dkim-signature"]
    for idx, value in enumerate(signatures):
        try:
            tags = parse_tag_value(value)
        except InvalidTagValueList:
            continue
        if tags.get(b"d", b"").strip().lower() not in wanted or b"l" in tags:
            continue
        signed = {h.strip().lower() for h in tags.get(b"h", b"").split(b":")}
        if not signed >= REQUIRED_SIGNED_HEADERS:
            continue
        if _verify_one(verifier, idx, dnsfunc):
            return True
    return False


def _verify_one(verifier: dkim.DKIM, idx: int, dnsfunc: DnsFunc) -> bool:
    try:
        return bool(verifier.verify(idx=idx, dnsfunc=dnsfunc))
    except Exception:  # dkim raises on malformed keys/signatures
        return False


def _aar_records_pass(aar: str, domains: set[str]) -> bool:
    parts = [p.strip() for p in _COMMENT.sub(" ", aar).split(";")]
    if len(parts) < 3 or not parts[0].replace(" ", "").startswith("i="):
        return False
    authserv = parts[1].split()[0].lower() if parts[1].split() else ""
    if authserv != ARC_AUTHSERV:
        return False
    for result in parts[2:]:
        tokens = result.split()
        if not tokens or tokens[0].lower() != "dkim=pass":
            continue
        props = dict(t.split("=", 1) for t in tokens[1:] if "=" in t)
        domain = props.get("header.d", "").strip('"').lower()
        if not domain:
            domain = props.get("header.i", "").strip('"').rpartition("@")[2].lower()
        if domain in domains:
            return True
    return False


def arc_pass(raw: bytes, domains: Iterable[str], dnsfunc: DnsFunc) -> bool:
    try:
        cv, results, _comment = dkim.arc_verify(raw, dnsfunc=dnsfunc)
    except Exception:
        return False
    if cv != b"pass" or not results:
        return False
    top = max(results, key=lambda r: int(r.get("instance", 0)))
    if top.get("as-domain", b"").lower() != ARC_SEALER:
        return False
    if top.get("ams-domain", b"").lower() != ARC_SEALER:
        return False
    if not (top.get("as-valid") and top.get("ams-valid")):
        return False
    aar = top.get("aar-value", b"")
    text = aar.decode("utf-8", "replace") if isinstance(aar, bytes) else str(aar)
    return _aar_records_pass(text, {d.lower() for d in domains})


def signature_domains(raw: bytes, limit: int = 5) -> list[str]:
    """The ``d=`` domains of the message's DKIM signatures (no verification, no content): lets a
    new bank's signing domain be found from its first email instead of guessed."""
    try:
        verifier = dkim.DKIM(raw, minkey=1024)
    except Exception:
        return []
    out: list[str] = []
    for k, value in verifier.headers:
        if k.lower() != b"dkim-signature" or len(out) >= limit:
            continue
        try:
            d = parse_tag_value(value).get(b"d", b"").strip().lower().decode("ascii")
        except (InvalidTagValueList, UnicodeDecodeError):
            continue
        if re.fullmatch(r"[a-z0-9.-]{1,253}", d):
            out.append(d)
    return out


def bank_signature_id(raw: bytes, domains: Iterable[str]) -> str | None:
    """A stable id for "this exact bank-signed message": SHA-256 of the bank signature's ``b=``.

    For banks whose emails carry no transaction number (Golomt), this is the idempotency
    material: a replayed or re-forwarded copy has the same signature, a different genuine email
    never does, and nobody can mint a new one without the bank's key. Call it only after
    ``authenticate`` passed; it does not verify anything itself.
    """
    wanted = {d.lower().encode() for d in domains}
    try:
        verifier = dkim.DKIM(raw, minkey=1024)
    except Exception:
        return None
    for k, value in verifier.headers:
        if k.lower() != b"dkim-signature":
            continue
        try:
            tags = parse_tag_value(value)
        except InvalidTagValueList:
            continue
        if tags.get(b"d", b"").strip().lower() in wanted and tags.get(b"b"):
            b = re.sub(rb"\s+", b"", tags[b"b"])
            return hashlib.sha256(b).hexdigest()
    return None


def authenticate(raw: bytes, domains: Iterable[str], dnsfunc: DnsFunc) -> bool:
    domains = tuple(domains)
    return dkim_pass(raw, domains, dnsfunc) or arc_pass(raw, domains, dnsfunc)
