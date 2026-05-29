"""Phase 2a free tier — free_service_daily_usage daily quota ledger.

Revision ID: 034_free_service_daily_usage
Revises: 033_user_voice_cleanup_tracking
Create Date: 2026-05-29

Per-job daily ledger for the free-tier 1/day cap (Phase 2a Task 4, gate #3).
**Independent** of ``users.free_jobs_quota_*`` (free *plan* total). State machine
``reserved → consumed | released | expired``; daily cap = active(reserved|consumed)
rows per ``(user_id, usage_date)`` (Asia/Shanghai natural day). Mirrors
``032_express_clone_reservations``.

Indexes:
1. ``uq_free_daily_active_idem`` (partial UNIQUE where status='reserved'):
   idempotency fail-safe 2nd defense — at most one active reserved row per
   ``(user_id, create_idempotency_key)`` (CodeX P1: scoped per user).
2. ``idx_free_daily_user_date_status``: daily cap count query.
3. ``idx_free_daily_ttl_pending`` (partial where status='reserved'): inline-expire
   / TTL sweeper selection.

Not deployed here — applied in the Phase 2 deployment step alongside the launch.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "034_free_service_daily_usage"
down_revision: Union[str, None] = "033_user_voice_cleanup_tracking"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "free_service_daily_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("usage_date", sa.String(length=10), nullable=False),
        sa.Column("create_idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'reserved'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_reason", sa.String(length=64), nullable=True),
    )

    # 1. idempotency fail-safe (2nd defense to the users-row lock): at most one
    #    active(reserved) row per (user_id, create_idempotency_key). partial unique —
    #    consumed/released/expired rows do not hold the slot.
    op.create_index(
        "uq_free_daily_active_idem",
        "free_service_daily_usage",
        ["user_id", "create_idempotency_key"],
        unique=True,
        postgresql_where=sa.text("status = 'reserved'"),
    )

    # 2. daily cap count query (user_id + usage_date + status).
    op.create_index(
        "idx_free_daily_user_date_status",
        "free_service_daily_usage",
        ["user_id", "usage_date", "status"],
    )

    # 3. inline-expire / TTL sweeper selection (only reserved rows).
    op.create_index(
        "idx_free_daily_ttl_pending",
        "free_service_daily_usage",
        ["expires_at"],
        postgresql_where=sa.text("status = 'reserved'"),
    )


def downgrade() -> None:
    op.drop_index("idx_free_daily_ttl_pending", table_name="free_service_daily_usage")
    op.drop_index("idx_free_daily_user_date_status", table_name="free_service_daily_usage")
    op.drop_index("uq_free_daily_active_idem", table_name="free_service_daily_usage")
    op.drop_table("free_service_daily_usage")
