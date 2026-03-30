"""Add commercialization fields: user plan/role, job policy snapshot.

Revision ID: 002_commercialization
Revises: 001_baseline
Create Date: 2026-03-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002_commercialization"
down_revision: Union[str, None] = "001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Users: membership fields ---
    op.add_column("users", sa.Column("role", sa.String(16), server_default="user", nullable=False))
    op.add_column("users", sa.Column("plan_code", sa.String(16), server_default="free", nullable=False))
    op.add_column("users", sa.Column("free_jobs_quota_total", sa.Integer(), server_default="5", nullable=False))
    op.add_column("users", sa.Column("free_jobs_quota_used", sa.Integer(), server_default="0", nullable=False))

    # --- Jobs: execution policy snapshot ---
    op.add_column("jobs", sa.Column("service_mode", sa.String(16), nullable=True))
    op.add_column("jobs", sa.Column("tts_provider", sa.String(32), nullable=True))
    op.add_column("jobs", sa.Column("tts_model", sa.String(64), nullable=True))
    op.add_column("jobs", sa.Column("requires_review", sa.Boolean(), nullable=True))
    op.add_column("jobs", sa.Column("voice_clone_enabled", sa.Boolean(), nullable=True))
    op.add_column("jobs", sa.Column("voice_strategy", sa.String(32), nullable=True))
    op.add_column("jobs", sa.Column("plan_code_snapshot", sa.String(16), nullable=True))
    op.add_column("jobs", sa.Column("role_snapshot", sa.String(16), nullable=True))
    op.add_column("jobs", sa.Column("source_duration_seconds", sa.Float(), nullable=True))
    op.add_column("jobs", sa.Column("quota_cost", sa.Integer(), server_default="1", nullable=True))
    op.add_column("jobs", sa.Column("estimated_duration_seconds", sa.Float(), nullable=True))
    op.add_column("jobs", sa.Column("create_idempotency_key", sa.String(128), unique=True, nullable=True))
    # quota_state tracks quota lifecycle: none -> reserved -> committed / released
    op.add_column("jobs", sa.Column("quota_state", sa.String(16), server_default="none", nullable=False))


def downgrade() -> None:
    # Jobs
    op.drop_column("jobs", "quota_state")
    op.drop_column("jobs", "create_idempotency_key")
    op.drop_column("jobs", "estimated_duration_seconds")
    op.drop_column("jobs", "quota_cost")
    op.drop_column("jobs", "source_duration_seconds")
    op.drop_column("jobs", "role_snapshot")
    op.drop_column("jobs", "plan_code_snapshot")
    op.drop_column("jobs", "voice_strategy")
    op.drop_column("jobs", "voice_clone_enabled")
    op.drop_column("jobs", "requires_review")
    op.drop_column("jobs", "tts_model")
    op.drop_column("jobs", "tts_provider")
    op.drop_column("jobs", "service_mode")
    # Users
    op.drop_column("users", "free_jobs_quota_used")
    op.drop_column("users", "free_jobs_quota_total")
    op.drop_column("users", "plan_code")
    op.drop_column("users", "role")
