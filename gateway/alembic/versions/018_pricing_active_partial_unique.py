"""Audit P1-11c follow-up²: partial UNIQUE on pricing active row.

Revision ID: 018_pricing_active_partial_unique
Revises: 017_audit_unique_constraints
Create Date: 2026-05-08

Codex review of commit 6019beb caught a race that the v0 P1-11c fix
(UNIQUE on ``version``) does NOT close:

  Step 0: pricing_config_versions has v=N status=active.
  Step 1: Request A SELECTs active row → sees v=N.
  Step 2: Request B SELECTs active row → sees v=N (same snapshot).
  Step 3: A: UPDATE WHERE status='active' → archived (locks row v=N).
  Step 4: B: UPDATE WHERE status='active' → archived
            (waits on A's lock on v=N).
  Step 5: A: SELECT max(version) → N. INSERT v=N+1 status=active. COMMIT.
  Step 6: B's lock on v=N acquires. PostgreSQL re-checks the WHERE
            clause against v=N's latest version: it's now ``archived``,
            predicate fails, B skips it. B's UPDATE statement uses its
            own snapshot from step 4; that snapshot does NOT contain
            A's just-inserted v=N+1 (read after A's commit would, but
            B's UPDATE is already past its snapshot phase).
            → B archives 0 rows.
  Step 7: B: SELECT max(version) — this is a NEW statement at READ
            COMMITTED, so it gets a fresh snapshot that DOES see
            v=N+1. max=N+1.
  Step 8: B: INSERT v=N+2 status=active. The version UNIQUE is
            satisfied (different number from A's N+1). The implicit
            "single active row" invariant is NOT enforced by any
            constraint. → INSERT succeeds.
  Step 9: B commits. Table now has v=N+1 active AND v=N+2 active.

Symptom: gateway pricing has multiple "active" rows. The
``select(...) WHERE status='active' ORDER BY desc(created_at) LIMIT 1``
query in pricing_admin.get_pricing / pricing_runtime.load returns
only one of them deterministically (the newest), but the schema is
broken: an admin browsing /api/admin/pricing/history sees two rows
with status='active' and the model becomes self-inconsistent.

Fix: partial UNIQUE index on ``(status)`` WHERE ``status = 'active'``.
PostgreSQL enforces "at most one row matching the predicate", so
B's INSERT in step 8 fails with ``IntegrityError`` even though its
``version`` differs from A's. The endpoint's existing
``except IntegrityError → 409`` handler catches it without needing
to disambiguate which constraint fired — concurrency conflict is
concurrency conflict, the user-visible response is the same.

Why partial UNIQUE rather than advisory lock:
* Declarative — invariant is in the schema, not split between schema
  and caller discipline.
* No lock to release; no risk of leaked locks on crash.
* Compatible with multi-process / multi-instance gateway deployments
  without coordinating a global lock service.
* Same pattern as ``idx_jobs_editing_touched_at`` (predicate index)
  already used in the project.

Downgrade is a clean ``op.drop_index``: the index is purely a
correctness barrier, no data dependency on it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "018_pricing_active_partial_unique"
down_revision: Union[str, None] = "017_audit_unique_constraints"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Index name follows the project convention ``uq_<table>_<predicate>``.
# This is technically a UNIQUE INDEX (not UNIQUE CONSTRAINT) because
# PostgreSQL only supports partial indexes, not partial constraints.
_INDEX_NAME = "uq_pricing_config_versions_active_status"


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        "pricing_config_versions",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        _INDEX_NAME,
        table_name="pricing_config_versions",
    )
