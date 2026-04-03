"""Add label_tasks table for async labeling queue.

Revision ID: 006_label_tasks
Revises: 005_add_voice_catalog
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "006_label_tasks"
down_revision = "005_voice_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "label_tasks",
        sa.Column("id", sa.String(20), primary_key=True),
        sa.Column("task_type", sa.String(50), nullable=False),
        sa.Column("label_type", sa.String(30), nullable=False),
        sa.Column("voice_ids", JSONB, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("progress_completed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("progress_total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("current_batch", sa.Integer, nullable=False, server_default="0"),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_lt_status", "label_tasks", ["status"])


def downgrade() -> None:
    op.drop_index("idx_lt_status")
    op.drop_table("label_tasks")
