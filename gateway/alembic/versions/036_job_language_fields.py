"""PR-A — jobs source/target language + language_pair columns.

Revision ID: 036_job_language_fields
Revises: 035_anonymous_preview
Create Date: 2026-06-13

Plan: docs/plans/2026-06-13-multilingual-mutual-translation-plan-v3.md §5 Phase 1.

Adds three additive, NOT NULL columns to ``jobs`` with a GA zero-regression
``server_default`` (en / zh-CN / en->zh-CN). These mirror the
``services.language_registry`` canonical codes + default pair and are kept in
lockstep with the gateway ``Job`` model and the Job API ``JobRecord`` dataclass.

Why this is safe on the hot, payment-active ``jobs`` table
----------------------------------------------------------
``ADD COLUMN ... NOT NULL DEFAULT <const>`` is a metadata-only operation on
PostgreSQL 11+ (no full-table rewrite; existing rows read the default lazily).
This mirrors migration 035's ``is_anonymous_preview`` add_column. No index is
created here — language_pair cost aggregation is PR-A part 2 (out of scope for
this slice).

Rollback is additive-only: downgrade drops the columns. Forward-rollout policy
(plan §5) keeps DB/code rolled forward and disables new pairs via Gateway
config rather than DB downgrade.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "036_job_language_fields"
down_revision: Union[str, None] = "035_anonymous_preview"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "source_language",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'en'"),
        ),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "target_language",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'zh-CN'"),
        ),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "language_pair",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'en->zh-CN'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("jobs", "language_pair")
    op.drop_column("jobs", "target_language")
    op.drop_column("jobs", "source_language")
