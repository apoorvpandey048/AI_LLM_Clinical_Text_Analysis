"""add user table and auth

Revision ID: 002
Revises: 001_custom_layers
Create Date: 2026-02-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import inspect as sa_inspect


# revision identifiers
revision = '002_add_users'
down_revision = '001_custom_layers'
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade():
    # Create users table (idempotent — may already exist from create_all())
    if not _table_exists('users'):
        op.create_table(
            'users',
            sa.Column('id', UUID(as_uuid=True), primary_key=True),
            sa.Column('username', sa.String(100), unique=True, nullable=False, index=True),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('password_hash', sa.String(255), nullable=False),
            sa.Column('role', sa.Enum('admin', 'doctor', 'pending', name='userrole'), default='pending', nullable=False),
            sa.Column('is_active', sa.Boolean(), default=True, nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.Column('last_login', sa.DateTime(), nullable=True),
        )

    # Add user_id to jobs table
    if not _has_column('jobs', 'user_id'):
        op.add_column('jobs', sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=True))
        op.create_index('ix_jobs_user_id', 'jobs', ['user_id'])


def downgrade():
    op.drop_index('ix_jobs_user_id', 'jobs')
    op.drop_column('jobs', 'user_id')
    op.drop_table('users')
    op.execute("DROP TYPE IF EXISTS userrole")
