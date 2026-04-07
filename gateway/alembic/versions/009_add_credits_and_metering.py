"""Add V3 credits system tables and job metering fields.

Revision ID: 009_credits_metering
Revises: 008_subscriptions
Create Date: 2026-04-07

V3-0: Job observation fields (estimated_minutes, actual_minutes, metering_snapshot)
V3-1: CreditsBucket + CreditsLedger tables for shadow ledger

Both additions are strictly additive. No existing tables or columns are altered
or dropped. The credits system operates in shadow mode — it does not gate job
execution or affect V2 billing/quota/entitlements.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "009_credits_metering"
down_revision: Union[str, None] = "008_subscriptions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- V3-0: Job observation fields ----------------------------------------
    op.add_column("jobs", sa.Column("estimated_minutes", sa.Float(), nullable=True))
    op.add_column("jobs", sa.Column("actual_minutes", sa.Float(), nullable=True))
    op.add_column("jobs", sa.Column("metering_snapshot", postgresql.JSONB(), nullable=True))

    # --- V3-1: credits_buckets -----------------------------------------------
    op.create_table(
        "credits_buckets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("bucket_type", sa.String(length=32), nullable=False),
        sa.Column("granted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("remaining", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_label", sa.String(length=64), nullable=True),
        sa.Column("related_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("related_subscription_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("idx_credits_buckets_user_id", "credits_buckets", ["user_id"])
    op.create_index("idx_credits_buckets_type", "credits_buckets", ["bucket_type"])

    # --- V3-1: credits_ledger ------------------------------------------------
    op.create_table(
        "credits_ledger",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "bucket_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("credits_buckets.id"),
            nullable=False,
        ),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("credits_delta", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("related_job_id", sa.String(length=64), nullable=True),
        sa.Column("related_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("related_subscription_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "reason_code",
            sa.String(length=64),
            nullable=False,
            server_default="unspecified",
        ),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("idx_credits_ledger_user_id", "credits_ledger", ["user_id"])
    op.create_index("idx_credits_ledger_bucket_id", "credits_ledger", ["bucket_id"])
    op.create_index("idx_credits_ledger_direction", "credits_ledger", ["direction"])
    op.create_index("idx_credits_ledger_created_at", "credits_ledger", ["created_at"])


def downgrade() -> None:
    # Drop ledger first (FK depends on buckets)
    op.drop_index("idx_credits_ledger_created_at", table_name="credits_ledger")
    op.drop_index("idx_credits_ledger_direction", table_name="credits_ledger")
    op.drop_index("idx_credits_ledger_bucket_id", table_name="credits_ledger")
    op.drop_index("idx_credits_ledger_user_id", table_name="credits_ledger")
    op.drop_table("credits_ledger")

    op.drop_index("idx_credits_buckets_type", table_name="credits_buckets")
    op.drop_index("idx_credits_buckets_user_id", table_name="credits_buckets")
    op.drop_table("credits_buckets")

    op.drop_column("jobs", "metering_snapshot")
    op.drop_column("jobs", "actual_minutes")
    op.drop_column("jobs", "estimated_minutes")
