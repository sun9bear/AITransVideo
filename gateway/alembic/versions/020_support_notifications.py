"""AI customer support + notification system tables (plan 2026-05-08).

Revision ID: 020_support_notifications
Revises: 019_add_phone_challenge_attempts
Create Date: 2026-05-08

Adds the data layer for the AI customer support handoff plan
(``docs/plans/2026-05-08-ai-customer-support-handoff-plan.md``):

- ``support_conversations`` — top-level conversation between user/visitor and
  the support system. Owns ``status`` and ``handoff_state``; carries the user
  context (``user_id`` / ``anonymous_id`` / ``page_url`` / ``job_id``) and the
  optional reference to whichever notification triggered it.
- ``support_messages`` — per-message log inside a conversation. Sender is one
  of ``user`` / ``assistant`` / ``human`` / ``system``. ``redacted_body`` holds
  the version that may be safely shown to a human agent (PII redacted).
- ``support_handoff_requests`` — one row per "escalate to human" attempt.
  Tracks provider (email / chatwoot / wechat_kf) and the status of the
  upstream call.
- ``support_ai_usage`` — ledger row written every time a conversation routes
  through a real LLM call (or, with cost=0, every time a template/FAQ short-
  circuits an LLM call). Drives the monthly budget accumulator.
- ``user_notifications`` — sanitized user-visible projection of pipeline
  events. ``scope`` separates ``system`` / ``user`` / ``job`` notifications.
  ``topic`` is orthogonal (billing / account / artifact / support / maintenance).
  ``dedupe_key`` + a partial unique index lets later phases add throttling
  without a schema change.

Hard contracts encoded here:
- All ``ON DELETE`` semantics: when a user is deleted, their support data
  cascades; when a conversation is deleted, its messages cascade.
- ``user_notifications`` does NOT cascade-delete from users — admin alerts
  and audit trails should outlive the user record. Account deletion runs a
  separate anonymization job that nulls ``user_id``/``body``/``title``.
- ``support_ai_usage.budget_month`` is the YYYY-MM string the accumulator
  groups by. Indexed for fast monthly sum queries.
- Severity / scope / topic / status fields stay as VARCHAR + check constraint
  (matches the existing convention in users.role / payment_orders.status etc.
  No Postgres ENUM types in this codebase).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "020_support_notifications"
down_revision: Union[str, None] = "019_add_phone_challenge_attempts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- support_conversations ---------------------------------------------
    op.create_table(
        "support_conversations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("anonymous_id", sa.String(64), nullable=True),
        sa.Column(
            "channel",
            sa.String(16),
            nullable=False,
            server_default="web",
        ),
        sa.Column("entrypoint", sa.String(64), nullable=True),
        sa.Column("page_url", sa.String(512), nullable=True),
        sa.Column("job_id", sa.String(64), nullable=True),
        sa.Column("category", sa.String(32), nullable=True),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="open",
        ),
        sa.Column(
            "handoff_state",
            sa.String(16),
            nullable=False,
            server_default="none",
        ),
        sa.Column("handoff_provider", sa.String(32), nullable=True),
        sa.Column("handoff_provider_conversation_id", sa.String(128), nullable=True),
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_confidence", sa.Float(), nullable=True),
        sa.Column(
            "message_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
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
        "idx_support_conversations_user_id",
        "support_conversations",
        ["user_id"],
    )
    op.create_index(
        "idx_support_conversations_anonymous_id",
        "support_conversations",
        ["anonymous_id"],
    )
    op.create_index(
        "idx_support_conversations_status",
        "support_conversations",
        ["status"],
    )
    op.create_index(
        "idx_support_conversations_created_at",
        "support_conversations",
        ["created_at"],
    )

    # --- support_messages ---------------------------------------------------
    op.create_table(
        "support_messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("support_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sender", sa.String(16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("redacted_body", sa.Text(), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "idx_support_messages_conversation_id",
        "support_messages",
        ["conversation_id"],
    )
    op.create_index(
        "idx_support_messages_created_at",
        "support_messages",
        ["created_at"],
    )

    # --- support_handoff_requests ------------------------------------------
    op.create_table(
        "support_handoff_requests",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("support_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "provider_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
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
        "idx_support_handoff_requests_conversation_id",
        "support_handoff_requests",
        ["conversation_id"],
    )
    op.create_index(
        "idx_support_handoff_requests_status",
        "support_handoff_requests",
        ["status"],
    )

    # --- support_ai_usage ---------------------------------------------------
    op.create_table(
        "support_ai_usage",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("support_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("anonymous_id", sa.String(64), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column(
            "input_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "output_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "input_usd_per_1m_tokens",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "output_usd_per_1m_tokens",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "estimated_cost_usd",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("budget_month", sa.String(7), nullable=False),
        sa.Column("route", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "idx_support_ai_usage_budget_month",
        "support_ai_usage",
        ["budget_month"],
    )
    op.create_index(
        "idx_support_ai_usage_conversation_id",
        "support_ai_usage",
        ["conversation_id"],
    )
    op.create_index(
        "idx_support_ai_usage_created_at",
        "support_ai_usage",
        ["created_at"],
    )

    # --- user_notifications -------------------------------------------------
    op.create_table(
        "user_notifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("topic", sa.String(32), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("job_id", sa.String(64), nullable=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "severity",
            sa.String(16),
            nullable=False,
            server_default="info",
        ),
        sa.Column("related_type", sa.String(32), nullable=True),
        sa.Column("related_id", sa.String(128), nullable=True),
        sa.Column("artifact_key", sa.String(64), nullable=True),
        sa.Column("action_url", sa.String(512), nullable=True),
        sa.Column("dedupe_key", sa.String(128), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "idx_user_notifications_user_id_created_at",
        "user_notifications",
        ["user_id", "created_at"],
    )
    op.create_index(
        "idx_user_notifications_user_id_unread",
        "user_notifications",
        ["user_id", "read_at"],
    )
    op.create_index(
        "idx_user_notifications_job_id",
        "user_notifications",
        ["job_id"],
    )
    op.create_index(
        "idx_user_notifications_scope",
        "user_notifications",
        ["scope"],
    )
    # Partial unique index for dedupe — only enforce uniqueness when
    # dedupe_key is set, so default NULL inserts never collide. The plan §16.2
    # promises P2 throttling can be added without a migration; this index is
    # the schema-side half of that promise.
    op.create_index(
        "uq_user_notifications_user_job_dedupe",
        "user_notifications",
        ["user_id", "job_id", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_user_notifications_user_job_dedupe",
        table_name="user_notifications",
    )
    op.drop_index(
        "idx_user_notifications_scope",
        table_name="user_notifications",
    )
    op.drop_index(
        "idx_user_notifications_job_id",
        table_name="user_notifications",
    )
    op.drop_index(
        "idx_user_notifications_user_id_unread",
        table_name="user_notifications",
    )
    op.drop_index(
        "idx_user_notifications_user_id_created_at",
        table_name="user_notifications",
    )
    op.drop_table("user_notifications")

    op.drop_index(
        "idx_support_ai_usage_created_at",
        table_name="support_ai_usage",
    )
    op.drop_index(
        "idx_support_ai_usage_conversation_id",
        table_name="support_ai_usage",
    )
    op.drop_index(
        "idx_support_ai_usage_budget_month",
        table_name="support_ai_usage",
    )
    op.drop_table("support_ai_usage")

    op.drop_index(
        "idx_support_handoff_requests_status",
        table_name="support_handoff_requests",
    )
    op.drop_index(
        "idx_support_handoff_requests_conversation_id",
        table_name="support_handoff_requests",
    )
    op.drop_table("support_handoff_requests")

    op.drop_index(
        "idx_support_messages_created_at",
        table_name="support_messages",
    )
    op.drop_index(
        "idx_support_messages_conversation_id",
        table_name="support_messages",
    )
    op.drop_table("support_messages")

    op.drop_index(
        "idx_support_conversations_created_at",
        table_name="support_conversations",
    )
    op.drop_index(
        "idx_support_conversations_status",
        table_name="support_conversations",
    )
    op.drop_index(
        "idx_support_conversations_anonymous_id",
        table_name="support_conversations",
    )
    op.drop_index(
        "idx_support_conversations_user_id",
        table_name="support_conversations",
    )
    op.drop_table("support_conversations")
