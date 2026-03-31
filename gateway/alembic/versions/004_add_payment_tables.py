"""Add payment_orders and payment_webhook_events tables.

Revision ID: 004_payment
Revises: 003_audit_log
Create Date: 2026-03-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision: str = "004_payment"
down_revision: Union[str, None] = "003_audit_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "payment_orders",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_order_id", sa.String(128), unique=True, nullable=True),
        sa.Column("target_plan_code", sa.String(16), nullable=False),
        sa.Column("billing_period", sa.String(16), server_default="monthly", nullable=False),
        sa.Column("amount_cny", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(8), server_default="CNY", nullable=False),
        sa.Column("status", sa.String(16), server_default="created", nullable=False),
        sa.Column("checkout_url", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_payment_orders_user_id", "payment_orders", ["user_id"])
    op.create_index("idx_payment_orders_status", "payment_orders", ["status"])

    op.create_table(
        "payment_webhook_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_event_id", sa.String(128), unique=True, nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("signature_valid", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("processed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("payload", JSONB(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("payment_webhook_events")
    op.drop_table("payment_orders")
