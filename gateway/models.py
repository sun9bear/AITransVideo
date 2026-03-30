"""SQLAlchemy models for users, sessions, and jobs."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="user"
    )  # "user" | "admin"
    plan_code: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="free"
    )  # "free" | "plus" | "pro"
    free_jobs_quota_total: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="5"
    )
    free_jobs_quota_used: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )


class Job(Base):
    """Job metadata — mirrors core fields from jobs/*.json, indexed by user_id."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("idx_jobs_user_id", "user_id"),
        Index("idx_jobs_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="youtube_url")
    source_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    speakers: Mapped[str] = mapped_column(String(8), nullable=False, default="auto")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    current_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_dir: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_gate: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    service_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)  # "express" | "studio"
    tts_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)  # "cosyvoice" | "minimax" | "mimo"
    tts_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requires_review: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    voice_clone_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    voice_strategy: Mapped[str | None] = mapped_column(String(32), nullable=True)  # "preset_mapping" | "user_selected"
    plan_code_snapshot: Mapped[str | None] = mapped_column(String(16), nullable=True)
    role_snapshot: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    quota_cost: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="1")
    estimated_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    create_idempotency_key: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True
    )
    quota_state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="none"
    )  # "none" | "reserved" | "committed" | "released"


class AdminAuditLog(Base):
    """Audit trail for admin actions on user entitlements."""

    __tablename__ = "admin_audit_log"
    __table_args__ = (
        Index("idx_audit_target_user", "target_user_id"),
        Index("idx_audit_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    admin_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    target_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # "update_role" | "update_plan_code" | "adjust_quota"
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    old_value: Mapped[str | None] = mapped_column(String(128), nullable=True)
    new_value: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class PaymentOrder(Base):
    """Payment order — tracks a single upgrade purchase."""

    __tablename__ = "payment_orders"
    __table_args__ = (
        Index("idx_payment_orders_user_id", "user_id"),
        Index("idx_payment_orders_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # "stripe" | "alipay" | "wechatpay" | "fake"
    provider_order_id: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True
    )
    target_plan_code: Mapped[str] = mapped_column(String(16), nullable=False)
    billing_period: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="monthly"
    )
    amount_cny: Mapped[int] = mapped_column(Integer, nullable=False)  # in fen (分)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, server_default="CNY")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="created"
    )  # "created" | "pending" | "paid" | "failed" | "cancelled" | "expired" | "refunded"
    checkout_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class PaymentWebhookEvent(Base):
    """Webhook event from payment provider — for idempotency and audit."""

    __tablename__ = "payment_webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
