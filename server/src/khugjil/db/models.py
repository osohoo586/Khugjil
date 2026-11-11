"""SQLAlchemy models. The authoritative DDL (including RLS) is migration 0001; a test checks that
these models and the migrated schema do not drift apart.

Every tenant-scoped table has ``tenant_id`` and ``UNIQUE (tenant_id, id)``; foreign keys between
tenant-scoped tables are composite ``(tenant_id, x_id)`` so the database itself rejects
cross-tenant links.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )


def _tenant() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), nullable=False)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _fk(column: str, target: str, ondelete: str = "CASCADE") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], ondelete=ondelete
    )


def _tenant_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE")


class Tenant(Base):
    __tablename__ = "tenant"
    __table_args__ = (
        CheckConstraint("status IN ('active','suspended')", name="tenant_status"),
        CheckConstraint("accent IN ('green','navy')", name="tenant_accent"),
        CheckConstraint("toast_seconds BETWEEN 5 AND 20", name="tenant_toast_seconds"),
        CheckConstraint("health_silence_hours BETWEEN 1 AND 72", name="tenant_health_hours"),
        CheckConstraint("shift_idle_hours BETWEEN 1 AND 24", name="tenant_shift_idle"),
    )
    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    accent: Mapped[str] = mapped_column(Text, nullable=False, server_default="green")
    include_own_transfers: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    toast_seconds: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="8")
    voice_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    voice_max_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    show_today_totals: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    health_silence_hours: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="4"
    )
    business_hours: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("""'{"days":[0,1,2,3,4,5,6],"start":"09:00","end":"21:00"}'::jsonb"""),
    )
    shift_idle_hours: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="8")
    shift_end_time: Mapped[time | None] = mapped_column(Time)
    min_consent_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = _now()


class TenantTermsAcceptance(Base):
    __tablename__ = "tenant_terms_acceptance"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk(),
        _fk("member_id", "member"),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    terms_version: Mapped[int] = mapped_column(Integer, nullable=False)
    staff_sharing_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    accepted_at: Mapped[datetime] = _now()


class InboundAddress(Base):
    __tablename__ = "inbound_address"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk(),
        CheckConstraint("status IN ('active','retired')", name="inbound_status"),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    local_part_hmac: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    local_part_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[datetime] = _now()
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ForwardingConfirmation(Base):
    __tablename__ = "forwarding_confirmation"
    __table_args__ = (UniqueConstraint("tenant_id", "id"), _tenant_fk())
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    code_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    received_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IngestEvent(Base):
    __tablename__ = "ingest_event"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk(),
        Index("ingest_event_tenant_time", "tenant_id", "received_at"),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    msgid_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = _now()


class BankAccount(Base):
    __tablename__ = "bank_account"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "number_hmac"),
        _tenant_fk(),
        CheckConstraint("status IN ('active','inactive')", name="bank_account_status"),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    number_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    number_hmac: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[datetime] = _now()


class RecipientGroup(Base):
    __tablename__ = "recipient_group"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk(),
        CheckConstraint("status IN ('active','inactive')", name="group_status"),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[datetime] = _now()


class GroupAccount(Base):
    __tablename__ = "group_account"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "group_id", "account_id"),
        _fk("group_id", "recipient_group"),
        _fk("account_id", "bank_account"),
    )
    tenant_id: Mapped[uuid.UUID] = _tenant()
    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


class Member(Base):
    __tablename__ = "member"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk(),
        CheckConstraint("role IN ('owner','admin','cashier','viewer')", name="member_role"),
        CheckConstraint("status IN ('invited','active','revoked')", name="member_status"),
        Index("member_login_name", text("lower(login_name)"), unique=True),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="invited")
    login_name: Mapped[str | None] = mapped_column(Text)
    password_hash: Mapped[str | None] = mapped_column(Text)
    totp_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger)
    totp_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    login_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    login_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pin_hash: Mapped[str | None] = mapped_column(Text)
    pin_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    pin_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GroupMember(Base):
    __tablename__ = "group_member"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "group_id", "member_id"),
        _fk("group_id", "recipient_group"),
        _fk("member_id", "member"),
    )
    tenant_id: Mapped[uuid.UUID] = _tenant()
    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


class RecoveryCode(Base):
    __tablename__ = "recovery_code"
    __table_args__ = (UniqueConstraint("tenant_id", "id"), _fk("member_id", "member"))
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    code_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Invite(Base):
    __tablename__ = "invite"
    __table_args__ = (UniqueConstraint("tenant_id", "id"), _fk("member_id", "member"))
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _now()


class DutyWindow(Base):
    __tablename__ = "duty_window"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk("member_id", "member"),
        CheckConstraint("weekday BETWEEN 0 AND 6", name="duty_weekday"),
        CheckConstraint("start_local < end_local", name="duty_order"),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    start_local: Mapped[time] = mapped_column(Time, nullable=False)
    end_local: Mapped[time] = mapped_column(Time, nullable=False)


class ConsentDocument(Base):
    """Global (not tenant-scoped); the app role may only read it."""

    __tablename__ = "consent_document"
    __table_args__ = (
        PrimaryKeyConstraint("kind", "version"),
        CheckConstraint("kind IN ('member','tenant_terms')", name="consent_doc_kind"),
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    locale: Mapped[str] = mapped_column(Text, nullable=False, server_default="mn")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime] = _now()


class Consent(Base):
    __tablename__ = "consent"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk("member_id", "member"),
        CheckConstraint("channel IN ('web','telegram')", name="consent_channel"),
        Index("consent_member", "tenant_id", "member_id"),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    accepted_at: Mapped[datetime] = _now()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_via: Mapped[str | None] = mapped_column(Text)


class Device(Base):
    __tablename__ = "device"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk(),
        CheckConstraint("kind IN ('web','agent')", name="device_kind"),
        CheckConstraint("status IN ('active','revoked')", name="device_status"),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="web")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    paired_at: Mapped[datetime] = _now()
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    agent_version: Mapped[str | None] = mapped_column(Text)


class DeviceGroup(Base):
    __tablename__ = "device_group"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "device_id", "group_id"),
        _fk("device_id", "device"),
        _fk("group_id", "recipient_group"),
    )
    tenant_id: Mapped[uuid.UUID] = _tenant()
    device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


class DeviceCredential(Base):
    __tablename__ = "device_credential"
    __table_args__ = (UniqueConstraint("tenant_id", "id"), _fk("device_id", "device"))
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PairingCode(Base):
    __tablename__ = "pairing_code"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk(),
        CheckConstraint("kind IN ('web','agent')", name="pairing_code_kind"),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    device_name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="web")
    group_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    short_code_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _now()


class Shift(Base):
    __tablename__ = "shift"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk("device_id", "device"),
        _fk("member_id", "member"),
        Index(
            "shift_one_open_per_device",
            "device_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    started_at: Mapped[datetime] = _now()
    last_activity_at: Mapped[datetime] = _now()
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(Text)


class PushSubscription(Base):
    __tablename__ = "push_subscription"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk("device_id", "device"),
        _fk("member_id", "member"),
        _fk("shift_id", "shift"),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    shift_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    endpoint_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth_secret_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = _now()


class TelegramLink(Base):
    __tablename__ = "telegram_link"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "member_id"),
        _fk("member_id", "member"),
        CheckConstraint("status IN ('active','revoked')", name="telegram_status"),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    chat_hmac: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    chat_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    linked_at: Mapped[datetime] = _now()


class TelegramLinkCode(Base):
    __tablename__ = "telegram_link_code"
    __table_args__ = (UniqueConstraint("tenant_id", "id"), _fk("member_id", "member"))
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    code_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Transaction(Base):
    __tablename__ = "transaction"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "bank_account_id"],
            ["bank_account.tenant_id", "bank_account.id"],
            ondelete="SET NULL",  # migration uses SET NULL (bank_account_id)
        ),
        _tenant_fk(),
        CheckConstraint("amount > 0", name="transaction_amount_positive"),
        Index("transaction_tenant_time", "tenant_id", "txn_at"),
        Index("transaction_tenant_seq", "tenant_id", "seq"),
    )
    id: Mapped[uuid.UUID] = _pk()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False, unique=True)
    tenant_id: Mapped[uuid.UUID] = _tenant()
    bank_account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    to_account_hmac: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    journal_hmac: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    idempotency_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    txn_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    description_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    parser_version: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = _now()


class Delivery(Base):
    __tablename__ = "delivery"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("transaction_id", "channel", "target_id"),
        _fk("transaction_id", "transaction"),
        CheckConstraint("channel IN ('sse','webpush','telegram')", name="delivery_channel"),
        CheckConstraint(
            "status IN ('pending','sent','acked','skipped','failed')", name="delivery_status"
        ),
        Index("delivery_tenant_status", "tenant_id", "status"),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    transaction_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Alert(Base):
    __tablename__ = "alert"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk(),
        Index(
            "alert_one_open_per_kind",
            "tenant_id",
            "kind",
            unique=True,
            postgresql_where=text("resolved_at IS NULL"),
        ),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    opened_at: Mapped[datetime] = _now()
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Session(Base):
    __tablename__ = "session"
    __table_args__ = (UniqueConstraint("tenant_id", "id"), _fk("member_id", "member"))
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    created_at: Mapped[datetime] = _now()
    last_seen_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stepup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_prefix: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk(),
        CheckConstraint("actor_kind IN ('member','operator','system')", name="audit_actor_kind"),
        Index("audit_tenant_time", "tenant_id", "at"),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    actor_kind: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    object_type: Mapped[str | None] = mapped_column(Text)
    object_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    object_label: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = _now()
    ip_prefix: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


TENANT_SCOPED_TABLES: tuple[str, ...] = tuple(
    t.name for t in Base.metadata.sorted_tables if "tenant_id" in t.c
)
