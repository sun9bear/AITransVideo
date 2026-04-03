"""SQLAlchemy model for label_tasks table."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models import Base


class LabelTask(Base):
    __tablename__ = "label_tasks"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    task_type: Mapped[str] = mapped_column(String(50), nullable=False)
    label_type: Mapped[str] = mapped_column(String(30), nullable=False)
    voice_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    progress_completed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    progress_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    current_batch: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
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
