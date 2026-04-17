"""Add background_tasks table for user-triggered async export tasks.

Revision ID: 014_background_tasks
Revises: 013_user_voice_speed_calibration
Create Date: 2026-04-17

Introduces the ``background_tasks`` queue for Export Tasks v1:
``materials_pack`` (zip packaging) and ``generate_video`` (FFmpeg mux).

Deliberately NOT reusing ``label_tasks``:
- ``label_tasks`` is admin voice-labeling infrastructure with different
  field semantics (voice_ids, label_type, chunked progress).
- ``background_tasks`` is user-scoped, param-fingerprinted, artifact-producing.

Paid-API tasks (voice_clone / tts_preview) are intentionally NOT candidates
for this queue — CLAUDE.md paid-API safety rule.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "014_background_tasks"
down_revision = "013_user_voice_speed_calibration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "background_tasks",
        sa.Column("id", sa.String(20), primary_key=True),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_type", sa.String(32), nullable=False),
        sa.Column("params", JSONB, nullable=False),
        sa.Column("params_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("progress", JSONB, nullable=True),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # Active-task unique constraint: at most ONE pending/running task per
    # (job_id, task_type, params_fingerprint). Enforces atomic dedupe at DB
    # level; the application layer catches IntegrityError and returns the
    # existing row. Completed/failed rows are excluded from the predicate so
    # future retries with the same fingerprint succeed.
    #
    # Both Postgres and SQLite support partial indexes; we emit the same
    # WHERE clause for both so tests against in-memory sqlite enforce the
    # same invariant as production.
    op.create_index(
        "idx_bg_tasks_active",
        "background_tasks",
        ["job_id", "task_type", "params_fingerprint"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
        sqlite_where=sa.text("status IN ('pending', 'running')"),
    )
    # User-scoped recent-list queries
    op.create_index(
        "idx_bg_tasks_user_updated",
        "background_tasks",
        ["user_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_bg_tasks_user_updated", table_name="background_tasks")
    op.drop_index("idx_bg_tasks_active", table_name="background_tasks")
    op.drop_table("background_tasks")
