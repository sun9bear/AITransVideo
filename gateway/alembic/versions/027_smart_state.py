"""Add Job.smart_state JSONB column for Smart MVP P2 skeleton.

Revision ID: 027_smart_state
Revises: 026_email_auth
Create Date: 2026-05-14

Skeleton for plan 2026-05-13 §4.2 / §4.3. The column carries the
Smart pipeline state machine snapshot (status / credits_policy /
reserved_credits_per_minute / handoff_stage / reason). Always NULL
for express/studio jobs — backward compatible.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "027_smart_state"
down_revision: Union[str, None] = "026_email_auth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("smart_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "smart_state")
