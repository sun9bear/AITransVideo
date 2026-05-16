"""Add source metadata fields to user_voices.

Revision ID: 028_user_voice_source_metadata
Revises: 027_smart_state
Create Date: 2026-05-16

Phase 1 of docs/plans/2026-05-16-voice-clone-library-reuse-plan.md:
record clone provenance so future phases can match personal voices by
same source video and speaker.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "028_user_voice_source_metadata"
down_revision: Union[str, None] = "027_smart_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user_voices", sa.Column("source_job_id", sa.String(length=64), nullable=True))
    op.add_column("user_voices", sa.Column("source_type", sa.String(length=32), nullable=True))
    op.add_column("user_voices", sa.Column("source_ref", sa.Text(), nullable=True))
    op.add_column("user_voices", sa.Column("source_content_hash", sa.String(length=255), nullable=True))
    op.add_column("user_voices", sa.Column("source_upload_md5", sa.String(length=64), nullable=True))
    op.add_column("user_voices", sa.Column("source_video_title", sa.String(length=512), nullable=True))
    op.add_column("user_voices", sa.Column("source_speaker_name", sa.String(length=200), nullable=True))
    op.add_column("user_voices", sa.Column("source_speaker_name_key", sa.String(length=200), nullable=True))
    op.add_column("user_voices", sa.Column("source_published_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_voices", sa.Column("source_content_summary", sa.Text(), nullable=True))
    op.add_column("user_voices", sa.Column("source_content_era", sa.String(length=100), nullable=True))
    op.add_column(
        "user_voices",
        sa.Column("source_content_tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("user_voices", sa.Column("clone_sample_seconds", sa.Float(), nullable=True))
    op.add_column(
        "user_voices",
        sa.Column("clone_sample_segment_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("user_voices", sa.Column("created_from", sa.String(length=32), nullable=True))

    op.create_index(
        "idx_user_voices_source_hash_speaker_id",
        "user_voices",
        ["user_id", "source_content_hash", "source_speaker_id"],
    )
    op.create_index(
        "idx_user_voices_source_hash_speaker_name",
        "user_voices",
        ["user_id", "source_content_hash", "source_speaker_name_key"],
    )
    op.create_index(
        "idx_user_voices_source_ref",
        "user_voices",
        ["user_id", "source_ref"],
    )


def downgrade() -> None:
    op.drop_index("idx_user_voices_source_ref", table_name="user_voices")
    op.drop_index("idx_user_voices_source_hash_speaker_name", table_name="user_voices")
    op.drop_index("idx_user_voices_source_hash_speaker_id", table_name="user_voices")

    op.drop_column("user_voices", "created_from")
    op.drop_column("user_voices", "clone_sample_segment_ids")
    op.drop_column("user_voices", "clone_sample_seconds")
    op.drop_column("user_voices", "source_content_tags")
    op.drop_column("user_voices", "source_content_era")
    op.drop_column("user_voices", "source_content_summary")
    op.drop_column("user_voices", "source_published_at")
    op.drop_column("user_voices", "source_speaker_name_key")
    op.drop_column("user_voices", "source_speaker_name")
    op.drop_column("user_voices", "source_video_title")
    op.drop_column("user_voices", "source_upload_md5")
    op.drop_column("user_voices", "source_content_hash")
    op.drop_column("user_voices", "source_ref")
    op.drop_column("user_voices", "source_type")
    op.drop_column("user_voices", "source_job_id")
