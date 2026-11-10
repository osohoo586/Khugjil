"""Inbound SMTP (aiosmtpd). Receives only; never relays.

* STARTTLS is required before MAIL FROM whenever a certificate is configured (always in
  production).
* Exactly one RCPT per session; unknown/retired addresses are rejected at RCPT time (check a).
* Messages over 256 KB are refused during DATA by aiosmtpd's size limit.
* Per-IP session rate limit in Redis.
* Content-level drops still answer 250, so Gmail never bounces mail back or disables
  forwarding; the outcome is recorded by the pipeline.
Log lines here contain only the peer IP and the SMTP reply code (PLAN Q15).
"""

from __future__ import annotations

import asyncio
import ssl
import time
from typing import Any

from aiosmtpd.smtp import SMTP, Envelope, Session

from khugjil.ingest.pipeline import IngestDeps, ingest, resolve_recipient
from khugjil.logs import get_logger
from khugjil.settings import MAX_MESSAGE_BYTES

RateLimiter = Any  # an object with ``async allow(key: str, limit: int, window_s: int) -> bool``


class IngestHandler:
    def __init__(self, deps: IngestDeps, limiter: RateLimiter, per_minute: int) -> None:
        self._deps = deps
        self._limiter = limiter
        self._per_minute = per_minute
        self._log = get_logger()

    def _peer(self, session: Session) -> str:
        peer = session.peer
        return str(peer[0]) if isinstance(peer, tuple) else str(peer)

    async def handle_MAIL(  # noqa: N802 - aiosmtpd hook name
        self, server: SMTP, session: Session, envelope: Envelope, address: str, options: list[str]
    ) -> str:
        ip = self._peer(session)
        if not await self._limiter.allow(f"smtp:{ip}", self._per_minute, 60):
            self._log.warning("smtp.rate_limited", peer_ip=ip, smtp_code=421)
            return "421 4.7.0 Too many connections, try later"
        envelope.mail_from = address
        envelope.mail_options.extend(options)
        return "250 OK"

    async def handle_RCPT(  # noqa: N802
        self, server: SMTP, session: Session, envelope: Envelope, address: str, options: list[str]
    ) -> str:
        if envelope.rcpt_tos:
            return "452 4.5.3 Too many recipients"
        if await resolve_recipient(self._deps, address) is None:
            ip = self._peer(session)
            await self._limiter.allow(f"smtp-bad-rcpt:{ip}", 1_000_000, 3600)
            self._log.warning("smtp.rcpt_rejected", peer_ip=ip, smtp_code=550)
            return "550 5.1.1 No such user"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(  # noqa: N802
        self, server: SMTP, session: Session, envelope: Envelope
    ) -> str:
        raw = envelope.original_content or b""
        rcpt = envelope.rcpt_tos[0] if envelope.rcpt_tos else ""
        envelope.original_content = None
        envelope.content = None
        if not isinstance(raw, bytes) or not rcpt:
            return "554 5.5.1 No valid recipients"
        await ingest(self._deps, rcpt, raw, transport="smtp")
        return "250 2.0.0 OK"


def tls_context(cert: str, key: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


class ReloadingTLS:
    """Reload the certificate periodically so Caddy's renewals are picked up."""

    def __init__(self, cert: str, key: str, every_s: int = 6 * 3600) -> None:
        self._cert, self._key, self._every = cert, key, every_s
        self._ctx = tls_context(cert, key)
        self._loaded = time.monotonic()

    def get(self) -> ssl.SSLContext:
        if time.monotonic() - self._loaded > self._every:
            self._ctx = tls_context(self._cert, self._key)
            self._loaded = time.monotonic()
        return self._ctx


async def start_server(
    handler: IngestHandler,
    *,
    host: str,
    port: int,
    hostname: str,
    tls: ssl.SSLContext | None,
) -> asyncio.Server:
    """Serve SMTP on the current event loop (shared with the DB pool and Redis client)."""

    def factory() -> SMTP:
        return SMTP(
            handler,
            hostname=hostname,
            ident="khugjil",
            data_size_limit=MAX_MESSAGE_BYTES,
            decode_data=False,
            enable_SMTPUTF8=True,
            tls_context=tls,
            require_starttls=tls is not None,
            timeout=60,
        )

    return await asyncio.get_running_loop().create_server(factory, host=host, port=port)
