"""Audit P1-11a + P1-11c: composite + scalar unique constraints.

Revision ID: 017_audit_unique_constraints
Revises: 016_extend_phone_challenge_code
Create Date: 2026-05-07

Two related fixes from the 2026-05-07 audit:

* **P1-11a (D-CRITICAL-4)**: ``payment_webhook_events.provider_event_id``
  was UNIQUE on its own. Provider event IDs are not globally unique
  across providers — Stripe, Alipay, WeChat Pay can each emit
  ``evt_ABC123`` independently. A future cross-provider collision would
  silently reject the second event as a duplicate, losing settlement.
  The fix is a composite UNIQUE on ``(provider, provider_event_id)`` so
  the dedup key incorporates which provider sent it.

* **P1-11c (D-HIGH-3)**: ``pricing_config_versions.version`` had no
  UNIQUE constraint. ``pricing_admin`` computes the next version as
  ``select(func.max(version)) + 1`` then INSERTs without locking — two
  admins clicking "Save Draft" simultaneously both read max=N and both
  insert version=N+1, leaving two rows with the same version number.
  Adding UNIQUE makes the second insert fail loudly (caller can retry)
  rather than corrupting the version sequence.

Companion code changes (same commit, since cross-PR drift would put
schema and code out of sync):
  * ``gateway/models.py`` — ``__table_args__`` updated for both tables.
  * ``gateway/billing.py`` — ``on_conflict_do_nothing(index_elements=...)``
    now uses the composite ``["provider", "provider_event_id"]``.

Downgrade restores the original single-field UNIQUE on
``provider_event_id`` and drops the new ``version`` UNIQUE. It is
**lossy if production has accumulated** rows where two providers share
the same ``provider_event_id`` — that becomes a constraint violation
on downgrade. Such collisions don't exist today (Alipay is the only
provider) but the asymmetry is documented for ops awareness.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "017_audit_unique_constraints"
down_revision: Union[str, None] = "016_extend_phone_challenge_code"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Constraint names. SQLAlchemy's column-level ``unique=True`` generates
# ``<table>_<column>_key`` by default for PostgreSQL — that's the
# pre-017 name we drop. The new composite name follows the project
# convention ``uq_<table>_<col1>_<col2>``.
_OLD_PWE_UNIQUE = "payment_webhook_events_provider_event_id_key"
_NEW_PWE_UNIQUE = "uq_payment_webhook_events_provider_event"
_NEW_PCV_UNIQUE = "uq_pricing_config_versions_version"


def upgrade() -> None:
    # --- P1-11a: payment_webhook_events composite unique ---
    # Drop the old single-field constraint, then add the composite.
    # If a future deploy ever needs to support a transitional
    # "both unique constraints active" phase for zero-downtime rolling
    # restart, that's a P2 follow-up — current single-instance compose
    # deploy doesn't need it.
    op.drop_constraint(
        _OLD_PWE_UNIQUE, "payment_webhook_events", type_="unique"
    )
    op.create_unique_constraint(
        _NEW_PWE_UNIQUE,
        "payment_webhook_events",
        ["provider", "provider_event_id"],
    )

    # --- P1-11c: pricing_config_versions.version unique ---
    # No data-quality precondition: the ``version`` column has been
    # serially-allocated by ``pricing_admin`` and existing rows are
    # already unique. Adding the constraint locks in the invariant.
    op.create_unique_constraint(
        _NEW_PCV_UNIQUE,
        "pricing_config_versions",
        ["version"],
    )


def downgrade() -> None:
    op.drop_constraint(
        _NEW_PCV_UNIQUE, "pricing_config_versions", type_="unique"
    )
    op.drop_constraint(
        _NEW_PWE_UNIQUE, "payment_webhook_events", type_="unique"
    )
    op.create_unique_constraint(
        _OLD_PWE_UNIQUE,
        "payment_webhook_events",
        ["provider_event_id"],
    )
