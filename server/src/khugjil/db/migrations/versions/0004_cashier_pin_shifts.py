"""Owner's choice (2026-11-21): a paired cashier device shows income without a PIN shift.

The column default keeps the original PIN shifts for code paths that create tenants directly;
existing tenants switch to the simpler mode here, and `khugjil tenant create` does the same.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenant ADD COLUMN cashier_pin_shifts BOOLEAN DEFAULT true NOT NULL")
    op.execute("UPDATE tenant SET cashier_pin_shifts = false")


def downgrade() -> None:
    op.execute("ALTER TABLE tenant DROP COLUMN cashier_pin_shifts")
