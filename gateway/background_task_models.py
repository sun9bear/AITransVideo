"""SQLAlchemy model for background_tasks table.

See migration ``014_add_background_tasks`` for the full schema rationale.
This table is separate from ``label_tasks`` by design — see the
Export Tasks v1 plan (docs/plans/2026-04-16-background-task-system-plan.md)
for non-goals.

The indexes here MUST stay aligned with migration 014. Production DDL is
driven by alembic; this declaration lets tests build the same schema
(incl. the partial unique index that enforces atomic dedupe) via
``BackgroundTask.__table__.create()``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from models import Base


class BackgroundTask(Base):
    __tablename__ = "background_tasks"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    job_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    params_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    progress: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        # Partial UNIQUE: at most one pending/running task per
        # (job_id, task_type, params_fingerprint). Catches races that slip
        # past the application-level fast-path check in create_task().
        # Both dialects honored so tests against SQLite exercise the same
        # constraint as Postgres.
        Index(
            "idx_bg_tasks_active",
            "job_id", "task_type", "params_fingerprint",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
            sqlite_where=text("status IN ('pending', 'running')"),
        ),
        Index(
            "idx_bg_tasks_user_updated",
            "user_id", "updated_at",
        ),
    )
