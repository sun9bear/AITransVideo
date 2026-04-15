"""SQLAlchemy models for the dynamic voice catalog.

Two tables:
- voice_catalog: voice metadata (provider, gender, verify status, etc.)
- voice_labels: labeling history (text labels, audio profiles, final labels)

Uses JSONB for provider_config and verify_status to support different TTS
providers without schema changes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models import Base


class VoiceCatalog(Base):
    __tablename__ = "voice_catalog"
    __table_args__ = (
        Index("idx_vc_provider_matchable", "provider", "matchable"),
        Index("idx_vc_provider_config", "provider_config", postgresql_using="gin"),
        Index("idx_vc_verify_status", "verify_status", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    voice_id: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    gender: Mapped[str | None] = mapped_column(String(20), nullable=True)
    language: Mapped[str] = mapped_column(String(20), nullable=False, server_default="zh")
    scene: Mapped[str | None] = mapped_column(String(50), nullable=True)
    matchable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    verify_status: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    verify_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    source: Mapped[str] = mapped_column(String(50), nullable=False, server_default="manual")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Voice speed calibration (migration 012) ---
    # Scalar fallback: average across calibrated models, or single-model value.
    chars_per_second: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Per-model values, e.g. {"speech-2.8-turbo": 4.32, "speech-2.8-hd": 4.18}.
    chars_per_second_by_model: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    speed_calibrated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class VoiceLabel(Base):
    __tablename__ = "voice_labels"
    __table_args__ = (
        Index("idx_vl_voice_type_current", "voice_id", "label_type", "is_current"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    voice_id: Mapped[str] = mapped_column(
        String(200),
        ForeignKey("voice_catalog.voice_id", ondelete="CASCADE"),
        nullable=False,
    )
    label_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    # Demographic labels (text + final)
    age_group: Mapped[str | None] = mapped_column(String(20), nullable=True)
    persona_style: Mapped[str | None] = mapped_column(String(30), nullable=True)
    energy_level: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Audio profile labels (audio_round* + final)
    pitch_level: Mapped[str | None] = mapped_column(String(10), nullable=True)
    warmth: Mapped[str | None] = mapped_column(String(10), nullable=True)
    authority: Mapped[str | None] = mapped_column(String(10), nullable=True)
    intimacy: Mapped[str | None] = mapped_column(String(10), nullable=True)
    brightness: Mapped[str | None] = mapped_column(String(10), nullable=True)
    maturity: Mapped[str | None] = mapped_column(String(20), nullable=True)
    delivery_style: Mapped[str | None] = mapped_column(String(30), nullable=True)
    texture_tags: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    childlike: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    labeled_by: Mapped[str | None] = mapped_column(String(50), nullable=True)
    labeled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
