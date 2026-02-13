"""Add custom layer and version selection columns.

New columns:
- prompt_templates: is_builtin, display_order, is_default_prompt, active_version_id
- job_cases: extra_layer_outputs, extra_layer_raw_outputs

Revision ID: 001_custom_layers
Revises:
Create Date: 2025-01-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy import inspect as sa_inspect

# revision identifiers, used by Alembic.
revision = "001_custom_layers"
down_revision = None
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    """Check if a column already exists (handles create_all() having run first)."""
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    """Add new columns for custom layers and version selection."""

    # -- prompt_templates --
    if not _has_column("prompt_templates", "is_builtin"):
        op.add_column(
            "prompt_templates",
            sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        )
    if not _has_column("prompt_templates", "display_order"):
        op.add_column(
            "prompt_templates",
            sa.Column("display_order", sa.Integer(), nullable=False, server_default=sa.text("99")),
        )
    if not _has_column("prompt_templates", "is_default_prompt"):
        op.add_column(
            "prompt_templates",
            sa.Column("is_default_prompt", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )
    if not _has_column("prompt_templates", "active_version_id"):
        op.add_column(
            "prompt_templates",
            sa.Column(
                "active_version_id",
                sa.UUID(as_uuid=True),
                nullable=True,
            ),
        )

    # -- job_cases --
    if not _has_column("job_cases", "extra_layer_outputs"):
        op.add_column(
            "job_cases",
            sa.Column("extra_layer_outputs", postgresql.JSONB(), nullable=True),
        )
    if not _has_column("job_cases", "extra_layer_raw_outputs"):
        op.add_column(
            "job_cases",
            sa.Column("extra_layer_raw_outputs", postgresql.JSONB(), nullable=True),
        )


def downgrade() -> None:
    """Remove custom layer columns."""
    op.drop_column("job_cases", "extra_layer_raw_outputs")
    op.drop_column("job_cases", "extra_layer_outputs")
    op.drop_column("prompt_templates", "active_version_id")
    op.drop_column("prompt_templates", "is_default_prompt")
    op.drop_column("prompt_templates", "display_order")
    op.drop_column("prompt_templates", "is_builtin")
