"""Add voice speed calibration columns to voice_catalog.

Revision ID: 012_voice_speed_calibration
Revises: 011_pricing_config_versions
Create Date: 2026-04-14

Adds three nullable columns to voice_catalog for per-voice TTS speed
calibration (part of the translation-duration-alignment plan):

- chars_per_second: Float, single "best" value for quick lookups
  (average across calibrated models, or single-model value for
  providers with one model).
- chars_per_second_by_model: JSONB, maps model identifier to chars/sec.
  e.g. {"speech-2.8-turbo": 4.32, "speech-2.8-hd": 4.18}
- speed_calibrated_at: DateTime(tz), timestamp of last calibration.

Strictly additive — all columns nullable, no backfill needed. Existing
rows keep NULL until the calibration script populates them. The
runtime treats NULL as "no catalog value, fall back to probe".
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "012_voice_speed_calibration"
down_revision: Union[str, None] = "011_pricing_config_versions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "voice_catalog",
        sa.Column("chars_per_second", sa.Float(), nullable=True),
    )
    op.add_column(
        "voice_catalog",
        sa.Column("chars_per_second_by_model", JSONB, nullable=True),
    )
    op.add_column(
        "voice_catalog",
        sa.Column("speed_calibrated_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial index on calibrated rows — useful when filtering "which
    # voices still need calibration" from the admin side.
    op.create_index(
        "idx_vc_speed_calibrated",
        "voice_catalog",
        ["speed_calibrated_at"],
        postgresql_where=sa.text("chars_per_second IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_vc_speed_calibrated", table_name="voice_catalog")
    op.drop_column("voice_catalog", "speed_calibrated_at")
    op.drop_column("voice_catalog", "chars_per_second_by_model")
    op.drop_column("voice_catalog", "chars_per_second")
