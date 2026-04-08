"""Add user_voices table for personal voice library.

Revision ID: 010_user_voices
Revises: 009_credits_metering
Create Date: 2026-04-07

Per-user voice library storing MiniMax cloned voices. Replaces the global
file-based voice_registry.json with PostgreSQL-backed per-user storage.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "010_user_voices"
down_revision: Union[str, None] = "009_credits_metering"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_voices",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("voice_id", sa.String(200), nullable=False),
        sa.Column("voice_type", sa.String(20), nullable=False, server_default="cloned"),
        sa.Column("provider", sa.String(50), nullable=False, server_default="minimax_voice_clone"),
        sa.Column("tts_provider", sa.String(50), nullable=True),
        sa.Column("platform", sa.String(50), nullable=True),
        sa.Column("label", sa.String(200), nullable=False, server_default=""),
        sa.Column("source_speaker_id", sa.String(100), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_user_voices_user_id", "user_voices", ["user_id"])
    op.create_unique_constraint("uq_user_voices_user_voice", "user_voices", ["user_id", "voice_id"])


def downgrade() -> None:
    op.drop_constraint("uq_user_voices_user_voice", "user_voices", type_="unique")
    op.drop_index("idx_user_voices_user_id", table_name="user_voices")
    op.drop_table("user_voices")
