"""Add popup flag to system announcements + per-user dismiss tracking.

Revision ID: 024_announcement_popup
Revises: 023_system_announcements
Create Date: 2026-05-09

Plan 2026-05-08 §16.7 follow-up — admin can mark a system announcement
as ``popup=true`` so it appears as a modal the first time each
recipient is logged in and visiting the site, in addition to the
quiet bell entry.

Schema:
- ``system_announcements.popup`` boolean (default false). Stored on
  the source row so clones inherit it.
- ``user_notifications.popup`` boolean (default false). Set at
  fan-out time from the announcement's flag. Also opens the door for
  non-announcement notifications (e.g. ``support.human_replied``) to
  popup later — same field works.
- ``user_notifications.popup_dismissed_at`` timestamp, nullable.
  Tracks "user saw the popup once" separately from ``read_at`` so
  closing the modal doesn't silently mark the underlying notification
  as read. The bell badge stays unread until the user explicitly
  reads from /notifications.

Why two columns and not just a JSON metadata field:
- The "give me unviewed popups for this user" query runs on every
  page navigation; a btree index on ``(user_id, popup,
  popup_dismissed_at)`` keeps that O(log n) instead of scanning JSONB.
- Clear schema vocabulary for the regression tests to verify.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "024_announcement_popup"
down_revision: Union[str, None] = "023_system_announcements"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "system_announcements",
        sa.Column(
            "popup",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "user_notifications",
        sa.Column(
            "popup",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "user_notifications",
        sa.Column(
            "popup_dismissed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Partial index: only unviewed popups. Lets the per-page-load
    # ``GET /api/notifications/popups`` query stay fast even when
    # popup notifications accumulate over months.
    op.create_index(
        "idx_user_notifications_active_popups",
        "user_notifications",
        ["user_id", "created_at"],
        postgresql_where=sa.text(
            "popup = true AND popup_dismissed_at IS NULL "
            "AND archived_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_user_notifications_active_popups",
        table_name="user_notifications",
    )
    op.drop_column("user_notifications", "popup_dismissed_at")
    op.drop_column("user_notifications", "popup")
    op.drop_column("system_announcements", "popup")
