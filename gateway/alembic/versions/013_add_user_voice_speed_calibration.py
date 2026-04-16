"""Add voice speed calibration columns to user_voices.

Revision ID: 013_user_voice_speed_calibration
Revises: 012_voice_speed_calibration
Create Date: 2026-04-15

Mirror of migration 012's calibration columns, but on the per-user
``user_voices`` table (cloned voices). Cloned voices are NEVER in
``voice_catalog`` (catalog is for system-provided voices), so without
this column the per-segment speed pipeline can't get a real cps for any
cloned voice and falls back to default 4.5 — which silently kills both
pre-rewrite estimation and Phase 2 voice_setting.speed for cloned-voice
jobs (Job job_6673fdf6cb4d4cc6aedc70bc48f8828e on 2026-04-15 hit this
exact pattern: 17 spurious pre-TTS rewrites + 30 S5 rewrites because
the clone's real cps was 3.34 but pipeline used 4.5).

Phase 4 UX adds a "测试语速" button in the personal voice library so the
user can pay ~CNY 0.06 to calibrate a cloned voice once and have it
benefit every subsequent job using that voice.

Strictly additive — all columns nullable, no backfill needed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "013_user_voice_speed_calibration"
down_revision: Union[str, None] = "012_voice_speed_calibration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_voices",
        sa.Column("chars_per_second", sa.Float(), nullable=True),
    )
    op.add_column(
        "user_voices",
        sa.Column("chars_per_second_by_model", JSONB, nullable=True),
    )
    op.add_column(
        "user_voices",
        sa.Column("speed_calibrated_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial index for "show me uncalibrated cloned voices" admin queries
    # and pipeline lookups that need to skip uncalibrated entries fast.
    op.create_index(
        "idx_uv_speed_calibrated",
        "user_voices",
        ["speed_calibrated_at"],
        postgresql_where=sa.text("chars_per_second IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_uv_speed_calibrated", table_name="user_voices")
    op.drop_column("user_voices", "speed_calibrated_at")
    op.drop_column("user_voices", "chars_per_second_by_model")
    op.drop_column("user_voices", "chars_per_second")
