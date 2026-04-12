"""Add pricing_config_versions table.

Revision ID: 011_pricing_config_versions
Revises: 010_user_voices
Create Date: 2026-04-09

Versioned pricing configuration for admin publish/draft/archive workflow.
Strictly additive — no destructive changes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "011_pricing_config_versions"
down_revision: Union[str, None] = "010_user_voices"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pricing_config_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column("change_note", sa.Text(), nullable=True),
        sa.Column("updated_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_pricing_config_versions_status", "pricing_config_versions", ["status"])
    op.create_index("ix_pricing_config_versions_version", "pricing_config_versions", ["version"])
    op.create_index("ix_pricing_config_versions_created_at", "pricing_config_versions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_pricing_config_versions_created_at", table_name="pricing_config_versions")
    op.drop_index("ix_pricing_config_versions_version", table_name="pricing_config_versions")
    op.drop_index("ix_pricing_config_versions_status", table_name="pricing_config_versions")
    op.drop_table("pricing_config_versions")
