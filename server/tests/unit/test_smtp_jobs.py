"""SMTP server and scheduled job tests."""

from __future__ import annotations

import asyncio
import datetime as dt
import smtplib
import socket
import ssl
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlalchemy import text, update

from khugjil.db.models import Shift, Tenant
from khugjil.db.tenancy import tenant_session
from khugjil.delivery.notifier import SendResult
from khugjil.events import RecordingEventBus
from khugjil.ingest.smtp import IngestHandler, ReloadingTLS, start_server, tls_context
from khugjil.jobs.cron import (
    auto_end_shifts,
    business_hours_between,
    cron_jobs,
    health_check,
    notify_alerts,
    purge,
)
from khugjil.ratelimit import RateLimiter
from tests.fixtures.emails import Mailer

pytestmark = pytest.mark.db

HOURS = {"days": [0, 1, 2, 3, 4, 5, 6], "start": "09:00", "end": "21:00"}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def self_signed(tmp: Path) -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "in.khugjil.test")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    c, k = tmp / "cert.pem", tmp / "key.pem"
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(c), str(k)


@pytest.fixture
async def smtp(world: Any, tmp_path: Path) -> AsyncIterator[tuple[int, Any]]:
    cert, key = self_signed(tmp_path)
    handler = IngestHandler(
        world.db.deps(world._enqueue), RateLimiter(world.db.redis), per_minute=5
    )
    port = free_port()
    server = await start_server(
        handler, host="127.0.0.1", port=port, hostname="in.khugjil.test", tls=tls_context(cert, key)
    )
    try:
        yield port, world
    finally:
        server.close()
        await server.wait_closed()


def _client(port: int, *, starttls: bool = True) -> smtplib.SMTP:
    c = smtplib.SMTP("127.0.0.1", port, timeout=10)
    c.ehlo("mail-test.example")
    if starttls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        c.starttls(context=ctx)
        c.ehlo("mail-test.example")
    return c


async def test_smtp_accepts_valid_mail(smtp: Any, mailer: Mailer) -> None:
    port, world = smtp
    raw = mailer.bank_mail()

    def deliver() -> None:
        with _client(port) as c:
            c.sendmail("fwd@gmail.example", [world.inbound_address], raw)

    await asyncio.to_thread(deliver)
    assert await world.count("transaction") == 1


async def test_smtp_rejects_unknown_rcpt_and_second_rcpt(smtp: Any, mailer: Mailer) -> None:
    port, world = smtp

    def attempt() -> tuple[int, int, int]:
        with _client(port) as c:
            c.mail("fwd@gmail.example")
            bad = c.rcpt("nobody@in.khugjil.test")[0]
            good = c.rcpt(world.inbound_address)[0]
            second = c.rcpt(world.inbound_address)[0]
            c.rset()
            return bad, good, second

    assert await asyncio.to_thread(attempt) == (550, 250, 452)


async def test_smtp_requires_starttls(smtp: Any) -> None:
    port, _ = smtp

    def attempt() -> int:
        with _client(port, starttls=False) as c:
            return int(c.mail("fwd@gmail.example")[0])

    assert await asyncio.to_thread(attempt) == 530


async def test_smtp_size_limit(smtp: Any, mailer: Mailer) -> None:
    port, world = smtp
    raw = mailer.bank_mail() + b"x" * (300 * 1024)

    def attempt() -> None:
        with _client(port) as c:
            c.sendmail("fwd@gmail.example", [world.inbound_address], raw)

    with pytest.raises(smtplib.SMTPException):
        await asyncio.to_thread(attempt)
    assert await world.count("transaction") == 0


async def test_smtp_rate_limit(smtp: Any) -> None:
    port, _ = smtp

    def attempt() -> list[int]:
        codes = []
        with _client(port) as c:
            for _ in range(7):
                codes.append(c.mail("fwd@gmail.example")[0])
                c.rset()
        return codes

    codes = await asyncio.to_thread(attempt)
    assert codes[:5] == [250] * 5
    assert 421 in codes


def test_reloading_tls(tmp_path: Path) -> None:
    cert, key = self_signed(tmp_path)
    r = ReloadingTLS(cert, key, every_s=0)
    first = r.get()
    assert r.get() is not first


# ---------------------------------------------------------------- jobs


def test_business_hours() -> None:
    mon_10 = datetime(2026, 10, 12, 2, 0, tzinfo=UTC)  # Monday 10:00 local
    assert business_hours_between(mon_10, mon_10 + timedelta(hours=3), HOURS) == 3
    # Overnight: 20:00 -> next day 10:00 local counts 1h + 1h.
    eve = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    assert business_hours_between(eve, eve + timedelta(hours=14), HOURS) == 2
    weekdays = {**HOURS, "days": [0, 1, 2, 3, 4]}
    sat = datetime(2026, 10, 17, 2, 0, tzinfo=UTC)
    assert business_hours_between(sat, sat + timedelta(hours=10), weekdays) == 0
    assert business_hours_between(sat, sat, HOURS) == 0


async def test_health_check_opens_alert_once(world: Any, mailer: Mailer) -> None:
    now = datetime.now(UTC)
    assert await health_check(world.db.sm, now) == []
    later = now + timedelta(days=2)
    assert await health_check(world.db.sm, later) == [world.tenant_id]
    assert await health_check(world.db.sm, later) == []
    (alert,) = await world.rows("alert")
    assert alert["kind"] == "forwarding_silent"
    await world.ingest(mailer.bank_mail())
    (alert,) = await world.rows("alert")
    assert alert["resolved_at"] is not None


async def test_auto_end_shift_inactivity_and_schedule(world: Any) -> None:
    bus = RecordingEventBus()
    now = datetime.now(UTC)
    assert await auto_end_shifts(world.db.sm, bus, now) == 0
    async with tenant_session(world.db.sm, world.tenant_id) as s:
        await s.execute(update(Shift).values(last_activity_at=now - timedelta(hours=9)))
    assert await auto_end_shifts(world.db.sm, bus, now) == 1
    (shift,) = await world.rows("shift")
    assert shift["end_reason"] == "inactivity"
    assert bus.events[0][1]["type"] == "shift_ended"


async def test_auto_end_shift_scheduled(world: Any) -> None:
    now = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)  # 23:00 local
    async with tenant_session(world.db.sm, world.tenant_id) as s:
        await s.execute(update(Tenant).values(shift_end_time=dtime(22, 0)))
        await s.execute(
            update(Shift).values(
                started_at=now - timedelta(hours=3), last_activity_at=now - timedelta(minutes=5)
            )
        )
    assert await auto_end_shifts(world.db.sm, RecordingEventBus(), now) == 1
    (shift,) = await world.rows("shift")
    assert shift["end_reason"] == "scheduled"


async def test_auto_end_shift_duty_end(world: Any) -> None:
    now = datetime.now(UTC)
    async with tenant_session(world.db.sm, world.tenant_id) as s:
        await s.execute(
            text(
                "INSERT INTO duty_window (tenant_id, member_id, weekday, start_local, end_local) "
                "VALUES (:t, :m, :d, '00:00', '00:01')"
            ),
            {"t": world.tenant_id, "m": world.member_id, "d": (now.weekday() + 3) % 7},
        )
    assert await auto_end_shifts(world.db.sm, RecordingEventBus(), now) == 1
    (shift,) = await world.rows("shift")
    assert shift["end_reason"] == "duty_end"


async def test_purge_respects_retention(world: Any, mailer: Mailer) -> None:
    await world.ingest(mailer.bank_mail())
    now = datetime.now(UTC)
    counts = await purge(world.db.sm, world.db.settings, now)
    assert counts["transaction"] == 0
    counts = await purge(world.db.sm, world.db.settings, now + dt.timedelta(days=91))
    assert counts["transaction"] == 1
    assert counts["ingest_event"] == 1
    assert await world.count("transaction") == 0


async def test_notify_alerts_once(world: Any) -> None:
    sent: list[str] = []

    class FakeTelegram:
        async def send_text(self, target: Any, text: str) -> SendResult:
            sent.append(text)
            return SendResult.SENT

    async with tenant_session(world.db.sm, world.tenant_id) as s:
        await s.execute(
            text("UPDATE member SET role = 'owner' WHERE id = :m"), {"m": world.member_id}
        )
        await s.execute(
            text(
                "INSERT INTO telegram_link (tenant_id, member_id, chat_hmac, chat_enc) "
                "VALUES (:t, :m, '\\x01', '\\x02')"
            ),
            {"t": world.tenant_id, "m": world.member_id},
        )
        await s.execute(
            text("INSERT INTO alert (tenant_id, kind) VALUES (:t, 'template_changed')"),
            {"t": world.tenant_id},
        )
    assert await notify_alerts(world.db.sm, FakeTelegram(), datetime.now(UTC)) == 1
    assert await notify_alerts(world.db.sm, FakeTelegram(), datetime.now(UTC)) == 0
    assert "формат" in sent[0]


def test_cron_schedule_defined() -> None:
    assert len(cron_jobs()) == 4
