"""Owner's request (2026-11-21): more than one inbound address per tenant, each with a name.

The table already allowed several active addresses (the resolver matches any active HMAC); the
admin panel showed only the newest. A short owner-chosen name ("Төв салбарын Gmail") tells them
apart. It is not secret, so it is stored as plain text; the address itself stays encrypted.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE inbound_address ADD COLUMN label TEXT"
        " CONSTRAINT inbound_label_len CHECK (char_length(label) <= 60)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE inbound_address DROP COLUMN label")
