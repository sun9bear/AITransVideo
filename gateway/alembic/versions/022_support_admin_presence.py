"""Add support_admin_presence table for in-product human chat presence tracking.

Revision ID: 022_support_admin_presence
Revises: 021_sessions_expires_at_index
Create Date: 2026-05-08

Plan: docs/plans/2026-05-08-ai-customer-support-handoff-plan.md (L1 follow-up).

Tracks per-admin heartbeats so the support widget can route a user's
"转人工" click to either:
- in-product chat (when at least one admin's heartbeat is fresh), or
- WeChat QR fallback (when no admin is online — admin-uploaded QR is
  stored on disk; this table only tracks the boolean signal).

Schema rationale:
- One row per admin user — UPSERT on heartbeat. NOT a per-session row,
  because admins routinely have multiple tabs and we want a single
  "is this person available" view per identity.
- ``status`` covers ``online`` / ``paused`` / ``offline``. Paused means
  "logged in but don't route tickets to me" — the heartbeat keeps
  flowing so we know they're alive, but ``is_anyone_online()`` ignores
  paused rows.
- Index on ``(status, last_heartbeat_at)`` lets the online-check query
  filter and order in one pass without a sequential scan.

This is independent from ``sessions`` — sessions track auth lifetime,
presence tracks active attention. An admin can have a 30-day session
but not be at the keyboard right now.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "022_support_admin_presence"
down_revision: Union[str, None] = "021_sessions_expires_at_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "support_admin_presence",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="online",
        ),
        sa.Column(
            "last_heartbeat_at",
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
        "idx_support_admin_presence_status_heartbeat",
        "support_admin_presence",
        ["status", "last_heartbeat_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_support_admin_presence_status_heartbeat",
        table_name="support_admin_presence",
    )
    op.drop_table("support_admin_presence")
