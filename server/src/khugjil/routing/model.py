"""Immutable snapshot of everything eligibility depends on, loaded per transaction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from typing import Literal
from uuid import UUID

ConsentChannel = Literal["web", "telegram"]
DeviceKind = Literal["web", "agent"]
Role = Literal["owner", "admin", "cashier", "viewer"]
TargetKind = Literal["device", "telegram"]


@dataclass(frozen=True)
class ConsentState:
    version: int
    channel: ConsentChannel
    revoked: bool


@dataclass(frozen=True)
class DutyWindow:
    weekday: int  # Monday = 0, in Asia/Ulaanbaatar
    start: time
    end: time


@dataclass(frozen=True)
class MemberState:
    id: UUID
    active: bool
    role: Role
    consents: tuple[ConsentState, ...]
    duty: tuple[DutyWindow, ...]
    telegram_linked: bool


@dataclass(frozen=True)
class ShiftState:
    id: UUID
    member_id: UUID


@dataclass(frozen=True)
class DeviceState:
    id: UUID
    active: bool
    kind: DeviceKind
    open_shift: ShiftState | None


@dataclass(frozen=True)
class GroupState:
    id: UUID
    active: bool
    account_ids: frozenset[UUID]
    device_ids: frozenset[UUID]
    member_ids: frozenset[UUID]


@dataclass(frozen=True)
class RoutingSnapshot:
    min_consent_version: int
    account_id: UUID
    account_active: bool
    groups: tuple[GroupState, ...]
    devices: Mapping[UUID, DeviceState]
    members: Mapping[UUID, MemberState]


@dataclass(frozen=True)
class Target:
    kind: TargetKind
    target_id: UUID  # device id, or member id for Telegram
    member_id: UUID
    shift_id: UUID | None
