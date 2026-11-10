"""Consent and on-duty rules (SPEC §3.10, §6.5)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from zoneinfo import ZoneInfo

from khugjil.routing.model import ConsentChannel, ConsentState, DutyWindow
from khugjil.settings import BANK_TZ

_TZ = ZoneInfo(BANK_TZ)


def consent_active(
    consents: Iterable[ConsentState], channel: ConsentChannel, min_version: int
) -> bool:
    """Active = a non-revoked consent for this channel at or above the tenant's minimum version."""
    return any(
        c.channel == channel and not c.revoked and c.version >= min_version for c in consents
    )


def on_duty(windows: Iterable[DutyWindow], now: datetime) -> bool:
    """No schedule means always on duty; otherwise ``now`` must fall inside a window."""
    windows = tuple(windows)
    if not windows:
        return True
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(_TZ)
    t = local.time()
    return any(w.weekday == local.weekday() and w.start <= t < w.end for w in windows)
