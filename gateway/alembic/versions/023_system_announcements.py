"""Add system_announcements table for admin-composed broadcast notifications.

Revision ID: 023_system_announcements
Revises: 022_support_admin_presence
Create Date: 2026-05-08

Plan 2026-05-08 §16.7 follow-up — admin can compose a system
announcement targeted at one of 14 audience predicates (all / by plan /
by lifecycle / by behavior), preview the recipient count, send, and
later clone-and-resend.

Schema rationale:

- ``audience_kind`` is a coarse string (``"all"``, ``"plan_free"``,
  ``"trial_active"`` etc) — the resolver in
  ``gateway/system_announcements_service.py`` knows how to translate
  each kind into a SQL filter.
- ``audience_params`` is JSONB for the parameterized variants (``{"days":7}``
  for "registered_within_days", ``{"min_jobs":5}`` for "active_with_jobs").
- ``status`` covers ``draft`` / ``sent`` / ``archived``. We do NOT
  support ``scheduled`` in P1 (no background worker).
- ``recipient_count`` snapshots the size of the audience at send time.
  Useful for retro analytics and a sanity check ("did this go to the
  expected number of users?").
- ``parent_id`` lets a clone reference its source for "edit and resend"
  workflows. ON DELETE SET NULL because we don't want a soft-deleted
  parent to delete the clone.

The actual user_notifications fan-out happens at send time — one row
per recipient — so per-user read/archive state is automatic and the
existing notification center page renders these without changes. The
fan-out is keyed by ``related_type='system_announcement'`` +
``related_id=announcement_id`` so recall / dedupe queries stay simple.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "023_system_announcements"
down_revision: Union[str, None] = "022_support_admin_presence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "system_announcements",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "topic",
            sa.String(32),
            nullable=False,
            server_default="maintenance",
        ),
        sa.Column(
            "severity",
            sa.String(16),
            nullable=False,
            server_default="info",
        ),
        sa.Column("action_url", sa.String(512), nullable=True),
        sa.Column(
            "audience_kind",
            sa.String(32),
            nullable=False,
            server_default="all",
        ),
        sa.Column(
            "audience_params",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_by_admin_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "parent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("system_announcements.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recipient_count", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "idx_system_announcements_status",
        "system_announcements",
        ["status"],
    )
    op.create_index(
        "idx_system_announcements_created_at",
        "system_announcements",
        ["created_at"],
    )
    op.create_index(
        "idx_system_announcements_admin_id",
        "system_announcements",
        ["created_by_admin_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_system_announcements_admin_id", table_name="system_announcements")
    op.drop_index("idx_system_announcements_created_at", table_name="system_announcements")
    op.drop_index("idx_system_announcements_status", table_name="system_announcements")
    op.drop_table("system_announcements")
