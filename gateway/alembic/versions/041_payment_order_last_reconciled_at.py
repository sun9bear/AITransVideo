"""Merge production head with payment order reconciliation marker.

Revision ID: 041_payment_order_reconcile
Revises: 040_anon_claim_owner, 036_payment_order_reconcile
Create Date: 2026-06-17

The payment-order reconciliation column was first published as
``036_payment_order_reconcile`` on GitHub main before the repository was aligned
with the production database, which was already stamped at
``040_anon_claim_owner``. Keep the old 036 revision as the operation migration
and use this 041 revision only to merge both Alembic branches.
"""

from typing import Sequence, Union


revision: str = "041_payment_order_reconcile"
down_revision: Union[str, Sequence[str], None] = (
    "040_anon_claim_owner",
    "036_payment_order_reconcile",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
