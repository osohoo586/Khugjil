"""Initial schema: tables, forced row-level security, narrow cross-tenant lookups, grants.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import hashlib
import os
import re

from alembic import op
from sqlalchemy import text

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TABLES_DDL = [
    """
CREATE TABLE consent_document (
	kind TEXT NOT NULL,
	version INTEGER NOT NULL,
	locale TEXT DEFAULT 'mn' NOT NULL,
	body TEXT NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	published_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (kind, version),
	CONSTRAINT consent_doc_kind CHECK (kind IN ('member','tenant_terms'))
)
""",
    """
CREATE TABLE tenant (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	name TEXT NOT NULL,
	status TEXT DEFAULT 'active' NOT NULL,
	accent TEXT DEFAULT 'green' NOT NULL,
	include_own_transfers BOOLEAN DEFAULT false NOT NULL,
	toast_seconds SMALLINT DEFAULT '8' NOT NULL,
	voice_enabled BOOLEAN DEFAULT false NOT NULL,
	voice_max_amount NUMERIC(18, 2),
	show_today_totals BOOLEAN DEFAULT false NOT NULL,
	health_silence_hours SMALLINT DEFAULT '4' NOT NULL,
	business_hours JSONB DEFAULT '{"days":[0,1,2,3,4,5,6],"start":"09:00","end":"21:00"}'::jsonb NOT NULL,
	shift_idle_hours SMALLINT DEFAULT '8' NOT NULL,
	shift_end_time TIME WITHOUT TIME ZONE,
	min_consent_version INTEGER DEFAULT '1' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT tenant_status CHECK (status IN ('active','suspended')),
	CONSTRAINT tenant_accent CHECK (accent IN ('green','navy')),
	CONSTRAINT tenant_toast_seconds CHECK (toast_seconds BETWEEN 5 AND 20),
	CONSTRAINT tenant_health_hours CHECK (health_silence_hours BETWEEN 1 AND 72),
	CONSTRAINT tenant_shift_idle CHECK (shift_idle_hours BETWEEN 1 AND 24)
)
""",
    """
CREATE TABLE alert (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	kind TEXT NOT NULL,
	opened_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	notified_at TIMESTAMP WITH TIME ZONE,
	resolved_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE CASCADE
)
""",
    """
CREATE UNIQUE INDEX alert_one_open_per_kind ON alert (tenant_id, kind) WHERE resolved_at IS NULL
""",
    """
CREATE TABLE audit_log (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	actor_kind TEXT NOT NULL,
	actor_id UUID,
	action TEXT NOT NULL,
	object_type TEXT,
	object_id UUID,
	object_label TEXT,
	at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	ip_prefix TEXT,
	meta JSONB DEFAULT '{}'::jsonb NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE CASCADE,
	CONSTRAINT audit_actor_kind CHECK (actor_kind IN ('member','operator','system'))
)
""",
    """
CREATE INDEX audit_tenant_time ON audit_log (tenant_id, at)
""",
    """
CREATE TABLE bank_account (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	number_enc BYTEA NOT NULL,
	number_hmac BYTEA NOT NULL,
	alias TEXT NOT NULL,
	status TEXT DEFAULT 'active' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	UNIQUE (tenant_id, number_hmac),
	FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE CASCADE,
	CONSTRAINT bank_account_status CHECK (status IN ('active','inactive'))
)
""",
    """
CREATE TABLE device (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	name TEXT NOT NULL,
	kind TEXT DEFAULT 'web' NOT NULL,
	status TEXT DEFAULT 'active' NOT NULL,
	paired_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE,
	revoked_at TIMESTAMP WITH TIME ZONE,
	revoked_by UUID,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE CASCADE,
	CONSTRAINT device_kind CHECK (kind IN ('web','agent')),
	CONSTRAINT device_status CHECK (status IN ('active','revoked'))
)
""",
    """
CREATE TABLE forwarding_confirmation (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	code_enc BYTEA NOT NULL,
	received_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE CASCADE
)
""",
    """
CREATE TABLE inbound_address (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	local_part_hmac BYTEA NOT NULL,
	local_part_enc BYTEA NOT NULL,
	status TEXT DEFAULT 'active' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	retired_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE CASCADE,
	CONSTRAINT inbound_status CHECK (status IN ('active','retired')),
	UNIQUE (local_part_hmac)
)
""",
    """
CREATE TABLE ingest_event (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	reason_code TEXT NOT NULL,
	msgid_sha256 VARCHAR(64) NOT NULL,
	received_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE CASCADE
)
""",
    """
CREATE INDEX ingest_event_tenant_time ON ingest_event (tenant_id, received_at)
""",
    """
CREATE TABLE member (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	display_name TEXT NOT NULL,
	role TEXT NOT NULL,
	status TEXT DEFAULT 'invited' NOT NULL,
	login_name TEXT,
	password_hash TEXT,
	totp_secret_enc BYTEA,
	totp_last_step BIGINT,
	totp_enabled_at TIMESTAMP WITH TIME ZONE,
	login_failures INTEGER DEFAULT '0' NOT NULL,
	login_locked_until TIMESTAMP WITH TIME ZONE,
	pin_hash TEXT,
	pin_failures INTEGER DEFAULT '0' NOT NULL,
	pin_locked_until TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	revoked_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE CASCADE,
	CONSTRAINT member_role CHECK (role IN ('owner','admin','cashier','viewer')),
	CONSTRAINT member_status CHECK (status IN ('invited','active','revoked'))
)
""",
    """
CREATE UNIQUE INDEX member_login_name ON member (lower(login_name))
""",
    """
CREATE TABLE pairing_code (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	device_name TEXT NOT NULL,
	group_ids UUID[] NOT NULL,
	token_hash BYTEA NOT NULL,
	short_code_hash BYTEA NOT NULL,
	failed_attempts INTEGER DEFAULT '0' NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	used_at TIMESTAMP WITH TIME ZONE,
	created_by UUID,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE CASCADE,
	UNIQUE (token_hash),
	UNIQUE (short_code_hash)
)
""",
    """
CREATE TABLE recipient_group (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	name TEXT NOT NULL,
	status TEXT DEFAULT 'active' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE CASCADE,
	CONSTRAINT group_status CHECK (status IN ('active','inactive'))
)
""",
    """
CREATE TABLE consent (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	member_id UUID NOT NULL,
	version INTEGER NOT NULL,
	channel TEXT NOT NULL,
	accepted_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	revoked_at TIMESTAMP WITH TIME ZONE,
	revoked_via TEXT,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id, member_id) REFERENCES member (tenant_id, id) ON DELETE CASCADE,
	CONSTRAINT consent_channel CHECK (channel IN ('web','telegram'))
)
""",
    """
CREATE INDEX consent_member ON consent (tenant_id, member_id)
""",
    """
CREATE TABLE device_credential (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	device_id UUID NOT NULL,
	token_hash BYTEA NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id, device_id) REFERENCES device (tenant_id, id) ON DELETE CASCADE,
	UNIQUE (token_hash)
)
""",
    """
CREATE TABLE device_group (
	tenant_id UUID NOT NULL,
	device_id UUID NOT NULL,
	group_id UUID NOT NULL,
	PRIMARY KEY (tenant_id, device_id, group_id),
	FOREIGN KEY(tenant_id, device_id) REFERENCES device (tenant_id, id) ON DELETE CASCADE,
	FOREIGN KEY(tenant_id, group_id) REFERENCES recipient_group (tenant_id, id) ON DELETE CASCADE
)
""",
    """
CREATE TABLE duty_window (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	member_id UUID NOT NULL,
	weekday SMALLINT NOT NULL,
	start_local TIME WITHOUT TIME ZONE NOT NULL,
	end_local TIME WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id, member_id) REFERENCES member (tenant_id, id) ON DELETE CASCADE,
	CONSTRAINT duty_weekday CHECK (weekday BETWEEN 0 AND 6),
	CONSTRAINT duty_order CHECK (start_local < end_local)
)
""",
    """
CREATE TABLE group_account (
	tenant_id UUID NOT NULL,
	group_id UUID NOT NULL,
	account_id UUID NOT NULL,
	PRIMARY KEY (tenant_id, group_id, account_id),
	FOREIGN KEY(tenant_id, group_id) REFERENCES recipient_group (tenant_id, id) ON DELETE CASCADE,
	FOREIGN KEY(tenant_id, account_id) REFERENCES bank_account (tenant_id, id) ON DELETE CASCADE
)
""",
    """
CREATE TABLE group_member (
	tenant_id UUID NOT NULL,
	group_id UUID NOT NULL,
	member_id UUID NOT NULL,
	PRIMARY KEY (tenant_id, group_id, member_id),
	FOREIGN KEY(tenant_id, group_id) REFERENCES recipient_group (tenant_id, id) ON DELETE CASCADE,
	FOREIGN KEY(tenant_id, member_id) REFERENCES member (tenant_id, id) ON DELETE CASCADE
)
""",
    """
CREATE TABLE invite (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	member_id UUID NOT NULL,
	token_hash BYTEA NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	used_at TIMESTAMP WITH TIME ZONE,
	created_by UUID,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id, member_id) REFERENCES member (tenant_id, id) ON DELETE CASCADE,
	UNIQUE (token_hash)
)
""",
    """
CREATE TABLE recovery_code (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	member_id UUID NOT NULL,
	code_hash BYTEA NOT NULL,
	used_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id, member_id) REFERENCES member (tenant_id, id) ON DELETE CASCADE
)
""",
    """
CREATE TABLE session (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	member_id UUID NOT NULL,
	token_hash BYTEA NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	stepup_at TIMESTAMP WITH TIME ZONE,
	ip_prefix TEXT,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id, member_id) REFERENCES member (tenant_id, id) ON DELETE CASCADE,
	UNIQUE (token_hash)
)
""",
    """
CREATE TABLE shift (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	device_id UUID NOT NULL,
	member_id UUID NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	last_activity_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	ended_at TIMESTAMP WITH TIME ZONE,
	end_reason TEXT,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id, device_id) REFERENCES device (tenant_id, id) ON DELETE CASCADE,
	FOREIGN KEY(tenant_id, member_id) REFERENCES member (tenant_id, id) ON DELETE CASCADE
)
""",
    """
CREATE UNIQUE INDEX shift_one_open_per_device ON shift (device_id) WHERE ended_at IS NULL
""",
    """
CREATE TABLE telegram_link (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	member_id UUID NOT NULL,
	chat_hmac BYTEA NOT NULL,
	chat_enc BYTEA NOT NULL,
	status TEXT DEFAULT 'active' NOT NULL,
	linked_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	UNIQUE (tenant_id, member_id),
	FOREIGN KEY(tenant_id, member_id) REFERENCES member (tenant_id, id) ON DELETE CASCADE,
	CONSTRAINT telegram_status CHECK (status IN ('active','revoked')),
	UNIQUE (chat_hmac)
)
""",
    """
CREATE TABLE telegram_link_code (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	member_id UUID NOT NULL,
	code_hash BYTEA NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	used_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id, member_id) REFERENCES member (tenant_id, id) ON DELETE CASCADE,
	UNIQUE (code_hash)
)
""",
    """
CREATE TABLE tenant_terms_acceptance (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	member_id UUID NOT NULL,
	terms_version INTEGER NOT NULL,
	staff_sharing_confirmed BOOLEAN NOT NULL,
	accepted_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE CASCADE,
	FOREIGN KEY(tenant_id, member_id) REFERENCES member (tenant_id, id) ON DELETE CASCADE
)
""",
    """
CREATE TABLE transaction (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	seq BIGINT GENERATED ALWAYS AS IDENTITY,
	tenant_id UUID NOT NULL,
	bank_account_id UUID,
	to_account_hmac BYTEA NOT NULL,
	journal_hmac BYTEA NOT NULL,
	idempotency_key BYTEA NOT NULL,
	amount NUMERIC(18, 2) NOT NULL,
	currency VARCHAR(3) NOT NULL,
	txn_at TIMESTAMP WITH TIME ZONE NOT NULL,
	description_enc BYTEA NOT NULL,
	parser_version TEXT NOT NULL,
	received_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id, bank_account_id) REFERENCES bank_account (tenant_id, id) ON DELETE SET NULL (bank_account_id),
	FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE CASCADE,
	CONSTRAINT transaction_amount_positive CHECK (amount > 0),
	UNIQUE (seq),
	UNIQUE (idempotency_key)
)
""",
    """
CREATE INDEX transaction_tenant_seq ON transaction (tenant_id, seq)
""",
    """
CREATE INDEX transaction_tenant_time ON transaction (tenant_id, txn_at)
""",
    """
CREATE TABLE delivery (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	transaction_id UUID NOT NULL,
	channel TEXT NOT NULL,
	target_id UUID NOT NULL,
	status TEXT DEFAULT 'pending' NOT NULL,
	attempts INTEGER DEFAULT '0' NOT NULL,
	last_error_code TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	sent_at TIMESTAMP WITH TIME ZONE,
	acked_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	UNIQUE (transaction_id, channel, target_id),
	FOREIGN KEY(tenant_id, transaction_id) REFERENCES transaction (tenant_id, id) ON DELETE CASCADE,
	CONSTRAINT delivery_channel CHECK (channel IN ('sse','webpush','telegram')),
	CONSTRAINT delivery_status CHECK (status IN ('pending','sent','acked','skipped','failed'))
)
""",
    """
CREATE INDEX delivery_tenant_status ON delivery (tenant_id, status)
""",
    """
CREATE TABLE push_subscription (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	device_id UUID NOT NULL,
	member_id UUID NOT NULL,
	shift_id UUID NOT NULL,
	endpoint_enc BYTEA NOT NULL,
	p256dh TEXT NOT NULL,
	auth_secret_enc BYTEA NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id, device_id) REFERENCES device (tenant_id, id) ON DELETE CASCADE,
	FOREIGN KEY(tenant_id, member_id) REFERENCES member (tenant_id, id) ON DELETE CASCADE,
	FOREIGN KEY(tenant_id, shift_id) REFERENCES shift (tenant_id, id) ON DELETE CASCADE
)
""",
]

TENANT_SCOPED_TABLES = (
    "alert",
    "audit_log",
    "bank_account",
    "device",
    "forwarding_confirmation",
    "inbound_address",
    "ingest_event",
    "member",
    "pairing_code",
    "recipient_group",
    "consent",
    "device_credential",
    "device_group",
    "duty_window",
    "group_account",
    "group_member",
    "invite",
    "recovery_code",
    "session",
    "shift",
    "telegram_link",
    "telegram_link_code",
    "tenant_terms_acceptance",
    "transaction",
    "delivery",
    "push_subscription",
)

FUNCTIONS = [
    """
CREATE FUNCTION khj_current_tenant() RETURNS uuid
LANGUAGE sql STABLE
AS $$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $$
""",
    # Cross-tenant lookups. SECURITY DEFINER (owned by the migration role, which has BYPASSRLS
    # or is superuser) and returning ids only. Inputs are HMACs / hashes of secrets.
    """
CREATE FUNCTION khj_resolve_inbound(p_hmac bytea) RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
  SELECT a.tenant_id FROM inbound_address a JOIN tenant t ON t.id = a.tenant_id
  WHERE a.local_part_hmac = p_hmac AND a.status = 'active' AND t.status = 'active'
$$
""",
    """
CREATE FUNCTION khj_resolve_device_token(p_hash bytea)
RETURNS TABLE (tenant_id uuid, device_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
  SELECT c.tenant_id, c.device_id FROM device_credential c
  JOIN device d ON d.tenant_id = c.tenant_id AND d.id = c.device_id
  JOIN tenant t ON t.id = c.tenant_id
  WHERE c.token_hash = p_hash AND c.expires_at > now()
    AND d.status = 'active' AND t.status = 'active'
$$
""",
    """
CREATE FUNCTION khj_resolve_session(p_hash bytea)
RETURNS TABLE (tenant_id uuid, member_id uuid, session_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
  SELECT s.tenant_id, s.member_id, s.id FROM session s
  JOIN member m ON m.tenant_id = s.tenant_id AND m.id = s.member_id
  JOIN tenant t ON t.id = s.tenant_id
  WHERE s.token_hash = p_hash AND s.expires_at > now()
    AND m.status = 'active' AND t.status = 'active'
$$
""",
    """
CREATE FUNCTION khj_resolve_login(p_login text)
RETURNS TABLE (tenant_id uuid, member_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
  SELECT m.tenant_id, m.id FROM member m JOIN tenant t ON t.id = m.tenant_id
  WHERE lower(m.login_name) = lower(p_login) AND m.status = 'active' AND t.status = 'active'
$$
""",
    """
CREATE FUNCTION khj_resolve_pairing_token(p_hash bytea)
RETURNS TABLE (tenant_id uuid, pairing_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
  SELECT p.tenant_id, p.id FROM pairing_code p JOIN tenant t ON t.id = p.tenant_id
  WHERE p.token_hash = p_hash AND t.status = 'active'
$$
""",
    """
CREATE FUNCTION khj_resolve_pairing_short(p_hash bytea)
RETURNS TABLE (tenant_id uuid, pairing_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
  SELECT p.tenant_id, p.id FROM pairing_code p JOIN tenant t ON t.id = p.tenant_id
  WHERE p.short_code_hash = p_hash AND t.status = 'active'
$$
""",
    """
CREATE FUNCTION khj_resolve_invite(p_hash bytea)
RETURNS TABLE (tenant_id uuid, invite_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
  SELECT i.tenant_id, i.id FROM invite i JOIN tenant t ON t.id = i.tenant_id
  WHERE i.token_hash = p_hash AND t.status = 'active'
$$
""",
    """
CREATE FUNCTION khj_resolve_telegram_chat(p_hmac bytea)
RETURNS TABLE (tenant_id uuid, member_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
  SELECT l.tenant_id, l.member_id FROM telegram_link l JOIN tenant t ON t.id = l.tenant_id
  WHERE l.chat_hmac = p_hmac AND l.status = 'active' AND t.status = 'active'
$$
""",
    """
CREATE FUNCTION khj_resolve_telegram_code(p_hash bytea)
RETURNS TABLE (tenant_id uuid, code_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
  SELECT c.tenant_id, c.id FROM telegram_link_code c JOIN tenant t ON t.id = c.tenant_id
  WHERE c.code_hash = p_hash AND t.status = 'active'
$$
""",
    """
CREATE FUNCTION khj_active_tenants() RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$ SELECT id FROM tenant WHERE status = 'active' $$
""",
]

LOOKUP_FUNCTIONS = [
    "khj_current_tenant()",
    "khj_resolve_inbound(bytea)",
    "khj_resolve_device_token(bytea)",
    "khj_resolve_session(bytea)",
    "khj_resolve_login(text)",
    "khj_resolve_pairing_token(bytea)",
    "khj_resolve_pairing_short(bytea)",
    "khj_resolve_invite(bytea)",
    "khj_resolve_telegram_chat(bytea)",
    "khj_resolve_telegram_code(bytea)",
    "khj_active_tenants()",
]

MEMBER_CONSENT_V1 = """Мэдэгдэл хүлээн авах зөвшөөрөл (хувилбар 1)

Та энэхүү зөвшөөрлийг өгснөөр {tenant_name}-ийн бүртгэлтэй банкны дансанд орсон орлогын мэдэгдлийг ажлын ээлжийн үеэр хүлээн авна.

Танд харагдах мэдээлэл: орлогын дүн, валют, гүйлгээний утга, гүйлгээний огноо ба цаг, дансны товч нэр.

Систем огт хадгалдаггүй, харуулдаггүй мэдээлэл: төлөгчийн нэр, төлөгчийн дансны дугаар, дансны үлдэгдэл.

Хэнтэй хуваалцах вэ: зөвхөн {tenant_name}-ийн эрх бүхий ажилтнууд, зөвхөн ажлын ээлжийн үеэр. Ээлжээс гадуур танд мэдэгдэл илгээгдэхгүй.

Telegram-аар мэдэгдэл авбал мэдээлэл Telegram-ын серверээр дамжина. Утас, компьютерын дэлгэц түгжигдсэн үед ч системийн мэдэгдэл харагдаж болно.

Хадгалах хугацаа: гүйлгээний мэдээллийг {retention_days} хоногийн дараа автоматаар устгана.

Зөвшөөрлөө цуцлах: Та хүссэн үедээ байгууллагын админаас хүсэх эсвэл Telegram дээр /stop гэж бичих замаар зөвшөөрлөө цуцална. Цуцалсан даруйд мэдэгдэл ирэхээ болино."""

TENANT_TERMS_V1 = """Үйлчилгээний нөхцөл (хувилбар 1)

1. Энэ үйлчилгээ таны имэйлийн хайрцагт нэвтрэхгүй. Та зөвхөн Хаан банкны гүйлгээний мэдэгдлийн имэйлийг тусгай хаяг руу дамжуулах Gmail шүүлтүүр тохируулна.

2. Систем имэйлийн эх хувийг хадгалахгүй. Зөвхөн орлогын дүн, валют, огноо, гүйлгээний утгыг хадгална. Төлөгчийн нэр, дансны дугаар, дансны үлдэгдлийг хадгалахгүй.

3. Гүйлгээний мэдээллийг {retention_days} хоногийн дараа автоматаар устгана.

4. Та байгууллагын эзэмшигчийн хувьд энэ мэдээллийг өөрийн ажилтнуудтай хуваалцах эрхтэй гэдгээ баталж байна. Ажилтан бүр мэдэгдэл авахаас өмнө өөрөө зөвшөөрөл өгнө.

5. Та хүссэн үедээ дамжуулалтыг зогсоож, ажилтан, төхөөрөмжийн эрхийг цуцлах боломжтой."""


def _app_role() -> str:
    role = os.environ.get("KHJ_DB_APP_ROLE", "khugjil_app")
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
        raise ValueError("invalid KHJ_DB_APP_ROLE")
    return role


def upgrade() -> None:
    for stmt in TABLES_DDL:
        op.execute(stmt)
    for stmt in FUNCTIONS:
        op.execute(stmt)

    op.execute("ALTER TABLE tenant ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON tenant "
        "USING (id = khj_current_tenant()) WITH CHECK (id = khj_current_tenant())"
    )
    for table in TENANT_SCOPED_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY tenant_isolation ON "{table}" '
            "USING (tenant_id = khj_current_tenant()) "
            "WITH CHECK (tenant_id = khj_current_tenant())"
        )

    app = _app_role()
    op.execute(f"GRANT USAGE ON SCHEMA public TO {app}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {app}")
    op.execute(f"REVOKE UPDATE, DELETE ON audit_log FROM {app}")
    op.execute(f"REVOKE INSERT, UPDATE, DELETE ON consent_document FROM {app}")
    op.execute(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {app}")
    for fn in LOOKUP_FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {app}")

    insert = text(
        "INSERT INTO consent_document (kind, version, locale, body, sha256) "
        "VALUES (:kind, 1, 'mn', :body, :sha)"
    )
    for kind, body in (("member", MEMBER_CONSENT_V1), ("tenant_terms", TENANT_TERMS_V1)):
        sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        op.get_bind().execute(insert, {"kind": kind, "body": body, "sha": sha})


def downgrade() -> None:
    raise RuntimeError("the initial migration cannot be downgraded")
