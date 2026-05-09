"""Add per-artifact R2 publish registry to jobs table.

Revision ID: 025_add_r2_artifacts
Revises: 024_announcement_popup
Create Date: 2026-05-08

Plan: docs/plans/2026-05-07-disk-relief-via-r2-publisher-and-ttl.md §4.1

Two new columns on ``jobs`` to support proactive R2 push by the
gateway-side sweeper (see ``gateway/r2_artifact_sweeper.py``):

- ``r2_artifacts JSONB NULL`` — array of registry entries, one per
  artifact_key the publisher attempted. Each entry is a dict with
  shape::

    {
      "artifact_key": "publish.dubbed_video",
      "edit_generation": 0,
      "state": "pushed" | "already_present" | "skipped_missing" | "failed",
      "r2_key":          "...",  # only when state ∈ pushed/already_present
      "filename":        "...",  # same
      "content_type":    "...",  # same
      "size":            int,    # same
      "source_mtime_ns": int,    # same
      "error":           "...",  # only when state == failed
      "pushed_at":       ISO-8601 string,
    }

  ``NULL`` = sweeper has not processed this job yet (or the registry
  was reset by an editing/commit overwrite). The sweeper's primary
  candidate predicate is ``r2_artifacts IS NULL``.

- ``r2_push_retry_after TIMESTAMPTZ NULL`` — when the publisher last
  failed for this job, set to ``now + 5min`` so the sweeper backs off
  before retrying. ``NULL`` means "no backoff active".

Partial index ``idx_jobs_r2_push_pending`` covers the sweeper's hot
path (``WHERE r2_artifacts IS NULL``). Postgres only writes index
entries for matching rows so the cost stays proportional to the
pending population, not the full ``jobs`` table.

NOTE on rollback safety: dropping the column and the partial index
is reversible without data loss because both fields are additive
metadata. Production code that reads ``r2_artifacts`` must guard
``hasattr`` / ``getattr`` so a partial downgrade doesn't crash
gateway. The downgrade is intentionally simple — no data
backfill / migration is needed when reversing.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "025_add_r2_artifacts"
down_revision: Union[str, None] = "024_announcement_popup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("r2_artifacts", JSONB, nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "r2_push_retry_after",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Partial index: sweeper SELECT scans only NULL r2_artifacts rows.
    # ``completed_at`` is the order key — sweeper processes oldest
    # finished tasks first so the backfill window prefers older work.
    op.create_index(
        "idx_jobs_r2_push_pending",
        "jobs",
        ["completed_at"],
        postgresql_where=sa.text("r2_artifacts IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_jobs_r2_push_pending", table_name="jobs")
    op.drop_column("jobs", "r2_push_retry_after")
    op.drop_column("jobs", "r2_artifacts")
