"""Owner's choice: accept income to accounts not registered yet, adding them automatically.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenant ADD COLUMN auto_add_accounts BOOLEAN DEFAULT false NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE tenant DROP COLUMN auto_add_accounts")
