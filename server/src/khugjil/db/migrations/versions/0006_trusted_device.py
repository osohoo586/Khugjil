"""Owner's request (2026-11-21): a browser that signed in with a TOTP code is remembered.

For 30 days that browser asks only for the password at sign-in. Step-up for sensitive actions
always asks for the code, and re-inviting or revoking the member forgets every browser. The
browser holds a random 256-bit token in an httpOnly cookie; only its SHA-256 is stored here.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import os
import re

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def _app_role() -> str:
    role = os.environ.get("KHJ_DB_APP_ROLE", "khugjil_app")
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
        raise ValueError("invalid KHJ_DB_APP_ROLE")
    return role


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE trusted_device (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	tenant_id UUID NOT NULL,
	member_id UUID NOT NULL,
	token_hash BYTEA NOT NULL,
	label TEXT,
	ip_prefix TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	last_used_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id, member_id) REFERENCES member (tenant_id, id) ON DELETE CASCADE,
	UNIQUE (token_hash),
	CONSTRAINT trusted_device_label_len CHECK (char_length(label) <= 60)
)
"""
    )
    op.execute("ALTER TABLE trusted_device ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE trusted_device FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON trusted_device "
        "USING (tenant_id = khj_current_tenant()) "
        "WITH CHECK (tenant_id = khj_current_tenant())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON trusted_device TO {_app_role()}")


def downgrade() -> None:
    op.execute("DROP TABLE trusted_device")
