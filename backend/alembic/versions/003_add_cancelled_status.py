"""add cancelled to jobstatus enum

Revision ID: 003_add_cancelled_status
Revises: 002_add_users
Create Date: 2026-03-02
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = '003_add_cancelled_status'
down_revision = '002_add_users'
branch_labels = None
depends_on = None


def upgrade():
    # Add 'cancelled' value to the jobstatus enum type.
    # PostgreSQL requires ALTER TYPE … ADD VALUE which cannot run inside a transaction.
    # Use execution_options to run outside the default transaction.
    bind = op.get_bind()

    # Check if the value already exists (idempotent)
    result = bind.execute(
        sa.text(
            "SELECT 1 FROM pg_enum "
            "JOIN pg_type ON pg_enum.enumtypid = pg_type.oid "
            "WHERE pg_type.typname = 'jobstatus' AND pg_enum.enumlabel = 'cancelled'"
        )
    ).fetchone()

    if result is None:
        # Must run outside a transaction block
        bind.execute(sa.text("COMMIT"))
        bind.execute(sa.text("ALTER TYPE jobstatus ADD VALUE 'cancelled'"))


def downgrade():
    # PostgreSQL does not support removing enum values without recreating the type.
    # Safe no-op: the value simply goes unused.
    pass
