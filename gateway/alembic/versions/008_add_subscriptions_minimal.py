"""Add minimal subscription truth source and billing history tables.

Revision ID: 008_subscriptions
Revises: 007_phone_auth
Create Date: 2026-04-05

Task 4: split the billing layer into three explicit roles so later milestones
can build Billing UI and checkout on a stable truth source without tearing out
`PaymentOrder`:

- `payment_orders`          — existing checkout / webhook compatibility shell
- `payment_webhook_events`  — existing idempotency / audit record
- `subscriptions`           — NEW: current paid subscription truth
- `billing_invoices`        — NEW: user-visible billing history truth

The new tables are additive only. Nothing existing is dropped. Idempotency for
invoice writes comes from the unique constraint on `billing_invoices.payment_order_id`:
one paid order can only ever produce exactly one invoice row.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "008_subscriptions"
down_revision: Union[str, None] = "007_phone_auth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- subscriptions ----------------------------------------------------
    op.create_table(
        "subscriptions",
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
        sa.Column("plan_code", sa.String(length=16), nullable=False),
        sa.Column("billing_period", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="active",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "current_period_start", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index(
        "idx_subscriptions_user_id", "subscriptions", ["user_id"]
    )
    op.create_index(
        "idx_subscriptions_status", "subscriptions", ["status"]
    )
    # Partial unique index: at most one active row per user. DB-level guard
    # against concurrent paid settlements both inserting a second active row.
    op.create_index(
        "uq_subscriptions_one_active_per_user",
        "subscriptions",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    # --- billing_invoices -------------------------------------------------
    op.create_table(
        "billing_invoices",
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
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id"),
            nullable=True,
        ),
        sa.Column(
            "payment_order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("payment_orders.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_order_id", sa.String(length=128), nullable=True),
        sa.Column("plan_code", sa.String(length=16), nullable=False),
        sa.Column("billing_period", sa.String(length=16), nullable=False),
        sa.Column("amount_cny", sa.Integer(), nullable=False),
        sa.Column(
            "currency", sa.String(length=8), nullable=False, server_default="CNY"
        ),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="paid"
        ),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index(
        "idx_billing_invoices_user_id", "billing_invoices", ["user_id"]
    )
    op.create_index(
        "idx_billing_invoices_subscription_id",
        "billing_invoices",
        ["subscription_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_billing_invoices_subscription_id", table_name="billing_invoices"
    )
    op.drop_index("idx_billing_invoices_user_id", table_name="billing_invoices")
    op.drop_table("billing_invoices")

    op.drop_index(
        "uq_subscriptions_one_active_per_user", table_name="subscriptions"
    )
    op.drop_index("idx_subscriptions_status", table_name="subscriptions")
    op.drop_index("idx_subscriptions_user_id", table_name="subscriptions")
    op.drop_table("subscriptions")
