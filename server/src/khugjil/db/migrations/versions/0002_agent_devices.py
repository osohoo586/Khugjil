"""Phase 2 desktop agent: pairing codes carry the device kind; devices record the agent version.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE pairing_code ADD COLUMN kind TEXT DEFAULT 'web' NOT NULL")
    op.execute(
        "ALTER TABLE pairing_code ADD CONSTRAINT pairing_code_kind CHECK (kind IN ('web','agent'))"
    )
    op.execute("ALTER TABLE device ADD COLUMN agent_version TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE device DROP COLUMN agent_version")
    op.execute("ALTER TABLE pairing_code DROP CONSTRAINT pairing_code_kind")
    op.execute("ALTER TABLE pairing_code DROP COLUMN kind")
