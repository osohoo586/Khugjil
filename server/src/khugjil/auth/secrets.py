"""Passwords, PINs, TOTP and lockout policy.

argon2id for passwords and PINs; TOTP per RFC 6238 with ±1 step and replay protection.
"""

from __future__ import annotations

import base64
import hmac
import itertools
import os
from datetime import datetime, timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_HASHER = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=1)

MIN_PASSWORD_LENGTH = 12
COMMON_PASSWORDS = frozenset(
    {
        "123456789012", "password1234", "qwertyuiop12", "111111111111", "000000000000",
        "passwordpassword", "iloveyou1234", "adminadmin12", "qwerty123456", "123456123456",
        "mongolia1234", "ulaanbaatar1", "khanbank1234", "password123!", "welcome12345",
        "letmein12345", "abc123456789", "1q2w3e4r5t6y", "zaq12wsxcde3", "asdfghjkl123",
    }
)  # fmt: skip

LOCKOUT_THRESHOLD = 5
LOCKOUT_BASE = timedelta(minutes=15)
LOCKOUT_MAX = timedelta(hours=24)


class WeakSecretError(ValueError):
    pass


def hash_secret(value: str) -> str:
    return _HASHER.hash(value)


def verify_secret(stored: str | None, value: str) -> bool:
    if not stored:
        # Spend comparable time so missing hashes are not distinguishable by timing.
        _HASHER.hash(value)
        return False
    try:
        return _HASHER.verify(stored, value)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakSecretError("Нууц үг дор хаяж 12 тэмдэгттэй байх ёстой.")
    if password.lower() in COMMON_PASSWORDS or len(set(password)) < 4:
        raise WeakSecretError("Энэ нууц үг хэт энгийн байна. Өөр нууц үг сонгоно уу.")


def check_pin(pin: str) -> None:
    if len(pin) != 6 or not pin.isdigit():
        raise WeakSecretError("ПИН код 6 оронтой тоо байх ёстой.")
    digits = [int(c) for c in pin]
    steps = {b - a for a, b in itertools.pairwise(digits)}
    if len(set(pin)) <= 2 or steps in ({1}, {-1}) or pin in {"123123", "121212", "112233"}:
        raise WeakSecretError("Энэ ПИН код хэт энгийн байна. Өөр код сонгоно уу.")


def lockout_until(failures: int, now: datetime) -> datetime | None:
    """After every LOCKOUT_THRESHOLD failures, lock for 15 min, doubling, capped at 24 h."""
    if failures < LOCKOUT_THRESHOLD or failures % LOCKOUT_THRESHOLD:
        return None
    rounds = failures // LOCKOUT_THRESHOLD
    duration: timedelta = min(LOCKOUT_BASE * (2 ** (rounds - 1)), LOCKOUT_MAX)
    return now + duration


# ---------------------------------------------------------------- TOTP

TOTP_INTERVAL = 30


def new_totp_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode()


def totp_uri(secret: str, account: str, issuer: str = "Khugjil") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=issuer)


def verify_totp(secret: str, code: str, now: datetime, last_step: int | None) -> int | None:
    """Return the matched time step (store it as ``last_step``), or None. Replays are refused."""
    code = code.strip().replace(" ", "")
    if len(code) != 6 or not code.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    current = int(now.timestamp()) // TOTP_INTERVAL
    for step in (current - 1, current, current + 1):
        expected = totp.generate_otp(step)
        if hmac.compare_digest(expected, code) and (last_step is None or step > last_step):
            return step
    return None


def new_recovery_codes(n: int = 10) -> list[str]:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return [
        "-".join("".join(alphabet[b % 32] for b in os.urandom(5)) for _ in range(2))
        for _ in range(n)
    ]
