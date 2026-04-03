"""Add voice_catalog and voice_labels tables for dynamic voice library.

Revision ID: 005_voice_catalog
Revises: 004_payment
Create Date: 2026-04-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "005_voice_catalog"
down_revision: Union[str, None] = "004_payment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- voice_catalog ---
    op.create_table(
        "voice_catalog",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("voice_id", sa.String(200), unique=True, nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("provider_config", JSONB, nullable=False, server_default="{}"),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("gender", sa.String(20), nullable=True),
        sa.Column("language", sa.String(20), nullable=False, server_default="zh"),
        sa.Column("scene", sa.String(50), nullable=True),
        sa.Column("matchable", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("verify_status", JSONB, nullable=False, server_default="{}"),
        sa.Column("verify_attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("source", sa.String(50), nullable=False, server_default="manual"),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_vc_provider_matchable", "voice_catalog", ["provider", "matchable"])
    op.create_index("idx_vc_provider_config", "voice_catalog", ["provider_config"], postgresql_using="gin")
    op.create_index("idx_vc_verify_status", "voice_catalog", ["verify_status"], postgresql_using="gin")

    # --- voice_labels ---
    op.create_table(
        "voice_labels",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("voice_id", sa.String(200), sa.ForeignKey("voice_catalog.voice_id", ondelete="CASCADE"), nullable=False),
        sa.Column("label_type", sa.String(30), nullable=False),
        sa.Column("source_run_id", sa.String(100), nullable=True),
        sa.Column("is_current", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("age_group", sa.String(20), nullable=True),
        sa.Column("persona_style", sa.String(30), nullable=True),
        sa.Column("energy_level", sa.String(20), nullable=True),
        sa.Column("pitch_level", sa.String(10), nullable=True),
        sa.Column("warmth", sa.String(10), nullable=True),
        sa.Column("authority", sa.String(10), nullable=True),
        sa.Column("intimacy", sa.String(10), nullable=True),
        sa.Column("brightness", sa.String(10), nullable=True),
        sa.Column("maturity", sa.String(20), nullable=True),
        sa.Column("delivery_style", sa.String(30), nullable=True),
        sa.Column("texture_tags", JSONB, nullable=True),
        sa.Column("childlike", sa.Boolean, nullable=True),
        sa.Column("labeled_by", sa.String(50), nullable=True),
        sa.Column("labeled_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_vl_voice_type_current", "voice_labels", ["voice_id", "label_type", "is_current"])


def downgrade() -> None:
    op.drop_table("voice_labels")
    op.drop_table("voice_catalog")
