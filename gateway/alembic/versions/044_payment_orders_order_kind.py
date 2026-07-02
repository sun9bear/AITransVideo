"""CM-01 topup lane — payment_orders.order_kind discriminator.

Revision ID: 044_payment_orders_order_kind
Revises: 043_uservoice_compat_langs
Create Date: 2026-07-02

docs/plans/2026-07-02-commercialization-sprint-plan.md §2 CM-01.

Adds the "plan" | "topup" discriminator to payment_orders. Every existing row
is a plan-upgrade order → server_default backfills "plan" (NOT NULL safe).
The refund-fallback query (billing._highest_remaining_paid_order_for_user)
filters on order_kind == "plan" so a paid topup order can never be promoted
into user.plan_code; the settlement branch dispatches topup orders to
billing_topup.settle_topup_paid instead of subscription/plan upgrade.
"""

from typing import Union
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "044_payment_orders_order_kind"
down_revision: str | None = "043_uservoice_compat_langs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payment_orders",
        sa.Column(
            "order_kind",
            sa.String(length=16),
            nullable=False,
            server_default="plan",
        ),
    )


def downgrade() -> None:
    op.drop_column("payment_orders", "order_kind")
