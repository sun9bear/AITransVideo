"""Add payment order reconciliation marker.

Revision ID: 041_payment_order_reconcile
Revises: 040_anon_claim_owner
Create Date: 2026-06-17
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "041_payment_order_reconcile"
down_revision: Union[str, None] = "040_anon_claim_owner"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "payment_orders",
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_payment_orders_last_reconciled_at",
        "payment_orders",
        ["last_reconciled_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_payment_orders_last_reconciled_at",
        table_name="payment_orders",
    )
    op.drop_column("payment_orders", "last_reconciled_at")
