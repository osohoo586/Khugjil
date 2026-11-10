from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pyotp
import pytest

from khugjil.auth.devices import new_short_code, normalize_short_code
from khugjil.auth.secrets import (
    WeakSecretError,
    check_password,
    check_pin,
    hash_secret,
    lockout_until,
    new_recovery_codes,
    new_totp_secret,
    totp_uri,
    verify_secret,
    verify_totp,
)
from khugjil.auth.sessions import (
    create_session,
    csrf_token,
    csrf_valid,
    end_member_sessions,
    end_session,
    ip_prefix,
    load_session,
    mark_stepped_up,
)
from khugjil.crypto import Crypto

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "pin", ["123456", "654321", "111111", "121212", "112233", "12345", "abcdef"]
)
def test_weak_pins_rejected(pin: str) -> None:
    with pytest.raises(WeakSecretError):
        check_pin(pin)


def test_good_pin_and_password() -> None:
    check_pin("482913")
    check_password("hagas-tsag-uul-9")
    for bad in ("short", "aaaaaaaaaaaaaa", "password1234"):
        with pytest.raises(WeakSecretError):
            check_password(bad)


def test_hash_and_verify() -> None:
    h = hash_secret("482913")
    assert h.startswith("$argon2id$")
    assert verify_secret(h, "482913")
    assert not verify_secret(h, "482914")
    assert not verify_secret(None, "482913")
    assert not verify_secret("not-a-hash", "x")


def test_lockout_escalates_and_caps() -> None:
    assert lockout_until(4, NOW) is None
    assert lockout_until(5, NOW) == NOW + timedelta(minutes=15)
    assert lockout_until(6, NOW) is None
    assert lockout_until(10, NOW) == NOW + timedelta(minutes=30)
    assert lockout_until(100, NOW) == NOW + timedelta(hours=24)


def test_totp_window_and_replay() -> None:
    secret = new_totp_secret()
    totp = pyotp.TOTP(secret)
    code = totp.at(NOW)
    step = verify_totp(secret, code, NOW, None)
    assert step == int(NOW.timestamp()) // 30
    assert verify_totp(secret, code, NOW, step) is None  # replay
    prev = totp.at(NOW - timedelta(seconds=30))
    assert verify_totp(secret, prev, NOW, None) is not None
    old = totp.at(NOW - timedelta(seconds=90))
    assert verify_totp(secret, old, NOW, None) is None
    assert verify_totp(secret, "12ab56", NOW, None) is None
    assert "otpauth://" in totp_uri(secret, "owner")


def test_recovery_and_short_codes() -> None:
    codes = new_recovery_codes()
    assert len(set(codes)) == 10
    code = new_short_code()
    assert len(code) == 14
    assert code.count("-") == 2
    assert normalize_short_code("abcd-efgh-ijkl") == "ABCDEFGH1JK1"


def test_csrf_and_ip_prefix(crypto: Crypto) -> None:
    t = csrf_token(crypto, "session-token")
    assert csrf_valid(crypto, "session-token", t)
    assert not csrf_valid(crypto, "other", t)
    assert not csrf_valid(crypto, "session-token", None)
    assert ip_prefix("203.0.113.77") == "203.0.113.0/24"
    assert ip_prefix("2001:db8:1:2::5") == "2001:db8:1::/48"
    assert ip_prefix("nonsense") is None
    assert ip_prefix(None) is None


@pytest.mark.db
async def test_session_lifecycle(world: Any) -> None:
    sm = world.db.sm
    now = datetime.now(UTC)  # the resolver compares with the database clock
    token = await create_session(
        sm, world.tenant_id, world.member_id, now=now, ip="203.0.113.7", stepped_up=False
    )
    s = await load_session(sm, token, now + timedelta(minutes=5))
    assert s is not None
    assert not s.stepped_up
    await mark_stepped_up(sm, s, now + timedelta(minutes=6))
    s2 = await load_session(sm, token, now + timedelta(minutes=7))
    assert s2 is not None
    assert s2.stepped_up
    assert await load_session(sm, token, now + timedelta(minutes=40)) is None  # idle
    token2 = await create_session(sm, world.tenant_id, world.member_id, now=now, ip=None)
    s3 = await load_session(sm, token2, now)
    assert s3 is not None
    await end_session(sm, s3)
    assert await load_session(sm, token2, now) is None
    await create_session(sm, world.tenant_id, world.member_id, now=now, ip=None)
    await end_member_sessions(sm, world.tenant_id, world.member_id)
    assert await world.count("session") == 0
    assert await load_session(sm, "unknown", now) is None
