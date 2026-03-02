"""add is_regression_baseline to jobs

Revision ID: 003_regression_baseline
Revises: 002_add_users
Create Date: 2026-03-02
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


# revision identifiers
revision = '003_regression_baseline'
down_revision = '002_add_users'
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade():
    if not _has_column('jobs', 'is_regression_baseline'):
        op.add_column(
            'jobs',
            sa.Column('is_regression_baseline', sa.Boolean(), server_default='false', nullable=False),
        )


def downgrade():
    if _has_column('jobs', 'is_regression_baseline'):
        op.drop_column('jobs', 'is_regression_baseline')
