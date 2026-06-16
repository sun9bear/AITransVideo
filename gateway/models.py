"""SQLAlchemy models for users, sessions, and jobs."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # email + password_hash are nullable since Task 3 so phone-only accounts can
    # exist without a synthetic placeholder email. Legacy email login continues
    # to work for users that still have these fields populated.
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
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

    # --- Phone identity & trial bookkeeping (Task 3) ---
    # All four fields are nullable. `phone_number` is the normalized mainland-CN
    # form (leading "1", 11 digits) without "+86" or separators. It is unique so
    # the same handset cannot register twice.
    phone_number: Mapped[str | None] = mapped_column(
        String(32), unique=True, nullable=True
    )
    phone_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Trial bookkeeping. `trial_granted_at` is stamped the first time a phone
    # number passes verification; subsequent passes for the same phone never
    # re-grant. `trial_ends_at` stays NULL until the gateway `plan_catalog`
    # publishes concrete trial rules (days / source minutes). Task 3 intentionally
    # does NOT invent a value for it.
    trial_granted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trial_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PhoneVerificationChallenge(Base):
    """Single-use phone verification code.

    Persisted so OTP flows can survive gateway restarts and so tests can assert
    state transitions deterministically. Kept intentionally small: no provider
    registry, no notification bus — just enough to express one challenge.
    """

    __tablename__ = "phone_verification_challenges"
    __table_args__ = (
        Index("idx_phone_challenges_phone", "phone_number"),
        Index("idx_phone_challenges_expires", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    phone_number: Mapped[str] = mapped_column(String(32), nullable=False)
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    purpose: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="login"
    )  # "login" for Task 3; reserved for future "bind" / "reset" flows
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # P1-10a-2 / S-HIGH-4 (audit 2026-05-07, migration 019):
    # wrong-code attempts on this challenge. Pre-019 the
    # verify-code endpoint marked consumed_at on the FIRST wrong
    # guess, which let an attacker who knew a victim's phone
    # spam-burn the legitimate OTP and lock the victim out. The
    # post-019 logic: compare code first; on wrong guess increment
    # attempts and only consume when attempts reaches the limit
    # (default 3 — see ``MAX_VERIFY_ATTEMPTS`` in auth_phone).
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        default=0,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class EmailVerificationChallenge(Base):
    """Single-use email verification challenge.

    Used for email registration confirmation and email password reset. The
    verification code is stored as a password-style hash so database access
    alone is not enough to consume an active challenge.
    """

    __tablename__ = "email_verification_challenges"
    __table_args__ = (
        Index("idx_email_challenges_email", "email"),
        Index("idx_email_challenges_expires", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    purpose: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="registration"
    )  # "registration" | "password_reset"
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        default=0,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class Job(Base):
    """Job metadata — mirrors core fields from jobs/*.json, indexed by user_id."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("idx_jobs_user_id", "user_id"),
        Index("idx_jobs_status", "status"),
        # --- Post-edit infra (migration 015, 2026-04-18) ---
        # See docs/plans/2026-04-18-studio-post-edit-plan.md §5.1
        # root_job_id + user_id scope TTL lookup; expires_at is the ordering key.
        Index("idx_jobs_root_user_expires", "root_job_id", "user_id", "expires_at"),
        Index("idx_jobs_copy_of_job_id", "copy_of_job_id"),
        # Partial index used by editing_idle_scanner: most jobs never enter
        # editing, so NULL rows are excluded from the index. Mirrors migration
        # 015's CREATE INDEX so autogenerate does not propose creating it again.
        Index(
            "idx_jobs_editing_touched_at",
            "editing_touched_at",
            postgresql_where=text("editing_touched_at IS NOT NULL"),
        ),
        Index("idx_jobs_source_content_hash", "source_content_hash"),
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

    # --- Post-edit infra (migration 015, 2026-04-18) ---
    # See docs/plans/2026-04-18-studio-post-edit-plan.md §3.1
    # User-visible friendly title (auto-generated, user-editable, max 60 chars).
    # NULL → frontend falls back to getJobDisplayTitle(source_ref).
    display_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # Per-job TTL. NULL → cleanup uses legacy rule (created_at + 7d). For new
    # jobs, written at creation; for copies, computed via compute_copy_expires_at
    # (§5.1) as min(now + 7d, latest_live_sibling.expires_at + 24h).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Last user action in editing state. Updated on enter-edit + every mutation
    # (§5.4.1). idle_scanner cancels editing jobs idle > 24h.
    editing_touched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Direct parent of a copy (NULL for originals).
    copy_of_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Copy lineage root. Originals have root_job_id = job_id. Used with user_id
    # for TTL scope lookup — prevents cross-user TTL interference (D23).
    root_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Counter of editing → running → succeeded cycles. UI shows "正在重合成 · 第 N 次修改"
    # when > 0 (D33).
    edit_generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Identifies "same source video" for copy family lookup. Local upload: file
    # SHA-256; YouTube: "youtube:{video_id}". Used to associate copy families
    # with the original source (D23).
    source_content_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- R2 publish registry (migration 025, plan 2026-05-07) ---
    # Per-artifact registry written by gateway/r2_artifact_sweeper.py. NULL
    # means the sweeper has not processed this job yet (or an editing/commit
    # overwrite reset it). Each entry shape: see migration 025 docstring.
    # The download intercept (_resolve_r2_redirect) reads this directly so
    # downloads keep working after project_dir is cleaned up locally.
    #
    # ``none_as_null=True`` is mandatory: SQLAlchemy's default JSONB behavior
    # turns Python ``None`` assignment (e.g. the ``source_job.r2_artifacts =
    # None`` we run in ``_apply_editing_commit_gateway_side`` after an
    # overwrite) into a JSONB ``null`` literal, NOT a SQL NULL. The sweeper's
    # ``Job.r2_artifacts.is_(None)`` predicate (and the partial index on
    # ``WHERE r2_artifacts IS NULL``) can't match the literal, so the row
    # would be invisible to the sweeper while ORM reads still surface
    # ``None`` — the row gets stuck unable to receive a fresh push. With
    # ``none_as_null=True``, ``None`` round-trips as SQL NULL and both
    # planes agree. (Day 2 follow-up after observing 2 stuck post-edit
    # jobs in production.)
    r2_artifacts: Mapped[list[dict] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    # Set by the sweeper to ``now + 5min`` after a partial publish failure so
    # subsequent sweep passes back off this job. NULL = no backoff active.
    r2_push_retry_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- V3-0 observation fields (shadow metering) ---
    estimated_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    metering_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # metering_snapshot schema:
    #
    # --- LIVE (written by Gateway in V3-0/V3-1) ---
    #   "credits_estimated": int     # shadow estimate at reserve time
    #   "credits_actual": int        # shadow actual at capture time
    #   "service_mode": str          # "express" | "studio" (from job policy)
    #   "tts_provider": str          # from job policy snapshot
    #   "tts_model": str             # from job policy snapshot
    #
    # --- LIVE (written by Pipeline via POST /metering, V3-4) ---
    #   "final_cn_chars": int        # total CN chars in final translation
    #   "rewrite_triggered": bool    # whether any segment was rewritten
    #   "rewrite_count": int         # total rewrite operations
    #
    # --- LIVE_PARTIAL (V3-5: MiniMax/CosyVoice/VolcEngine; MiMo excluded) ---
    #   "tts_billed_chars": int      # provider billed chars (2x for MiniMax/CosyVoice, 1x for VolcEngine, 0 for MiMo)
    #
    # --- LIVE (V3-6: from compute_job_policy, current value: "standard") ---
    #   "quality_tier": str          # from policy at create time; "standard" | "high" | "flagship"

    # --- APF P0 anonymous preview (migration 035, 2026-06-10) ---
    # True for jobs spawned via the anonymous preview funnel; used by the
    # partial index ix_jobs_anon_preview_status for fast status queries on
    # preview jobs without touching the main jobs index.
    is_anonymous_preview: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )

    # --- Smart MVP P2 skeleton (plan 2026-05-13 §4.2 / §4.3) ---
    # Mirrors JobRecord.smart_state from Job API (synced via metering callback,
    # see job_intercept.update_job_metering allowed_keys whitelist). Read by
    # settle_job_credit_ledger smart dispatcher (§5.2). Always NULL for
    # express/studio jobs.
    smart_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


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
    )  # "created" | "pending" | "paid" | "failed" | "cancelled" | "expired" | "refunded" | "partial_refunded"
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

    # P1-11a (audit 2026-05-07, D-CRITICAL-4): dedup key is the COMPOSITE
    # ``(provider, provider_event_id)``, not provider_event_id alone.
    # Provider event IDs are not globally unique across providers (Stripe
    # / Alipay / WeChat Pay can each emit ``evt_ABC123`` independently);
    # a single-field UNIQUE risked silently dropping the second provider's
    # event. Migration 017 swaps the constraint at the schema level; this
    # __table_args__ keeps autogenerate / metadata consistent.
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_event_id",
            name="uq_payment_webhook_events_provider_event",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(
        String(128), nullable=False
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


class Subscription(Base):
    """Minimal paid-subscription truth record (Task 4).

    - One row per user represents the current paid plan commitment.
    - Trial state is NOT represented here: `users.trial_granted_at / trial_ends_at`
      remain the trial bookkeeping source of truth.
    - Usage ledger, team seats, reviewer seats, mandates, top-up balance are
      deliberately OUT OF SCOPE. Do not bolt them onto this table.
    - `status = "active"` is the only valid paid state for Task 4.
      Later milestones may introduce `past_due`, `cancelled`, `expired`, etc.
    """

    __tablename__ = "subscriptions"
    __table_args__ = (
        Index("idx_subscriptions_user_id", "user_id"),
        Index("idx_subscriptions_status", "status"),
        # Partial unique index: at most one row with status='active' per user.
        # Enforced at the DB layer so concurrent paid settlements for the same
        # user cannot both INSERT a new active subscription row. The losing
        # INSERT fails with IntegrityError, its transaction rolls back (which
        # also rolls back the matching PaymentWebhookEvent insert so the event
        # is not marked processed), and the provider can retry that callback.
        Index(
            "uq_subscriptions_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    plan_code: Mapped[str] = mapped_column(String(16), nullable=False)  # plus | pro
    billing_period: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # monthly | quarterly | annual
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # "alipay" | "wechatpay" | "fake" | future providers
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="active"
    )  # "active" | "past_due" | "cancelled" | "expired"
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    current_period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class BillingInvoice(Base):
    """User-visible billing history record (Task 4).

    One row per settled `PaymentOrder` — the unique index on `payment_order_id`
    is what makes webhook settlement idempotent from this table's point of view:
    a duplicate callback will find an existing row and update it in place
    instead of creating a second invoice.

    `PaymentOrder` still exists as the checkout / webhook compatibility shell;
    `billing_invoices` is the stable list that later Billing UI will read.
    """

    __tablename__ = "billing_invoices"
    __table_args__ = (
        Index("idx_billing_invoices_user_id", "user_id"),
        Index("idx_billing_invoices_subscription_id", "subscription_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=True
    )
    payment_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payment_orders.id"),
        unique=True,
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    plan_code: Mapped[str] = mapped_column(String(16), nullable=False)
    billing_period: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_cny: Mapped[int] = mapped_column(Integer, nullable=False)  # fen
    currency: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="CNY"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="paid"
    )  # "paid" | "failed" | "refunded" | "partial_refunded"
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# V3 Credits System — Shadow Mode (V3-0 / V3-1)
# ---------------------------------------------------------------------------


class CreditsBucket(Base):
    """Per-source credit pool for a user.

    Each bucket tracks credits from a single source (free grant, trial grant,
    subscription allowance, top-up purchase, admin adjustment). Multiple buckets
    may coexist for one user; consumption priority is enforced by
    ``credits_service`` based on ``bucket_type`` + ``service_mode``.

    V3-1 shadow mode: buckets are written and queryable but do NOT gate job
    execution. V2 quota / billing / entitlements remain the production truth.
    """

    __tablename__ = "credits_buckets"
    __table_args__ = (
        Index("idx_credits_buckets_user_id", "user_id"),
        Index("idx_credits_buckets_type", "bucket_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    bucket_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # "free" | "trial" | "subscription" | "topup" | "manual_adjustment"
    granted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remaining: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_label: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )  # e.g. "plus", "pro", "topup_1000"
    related_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    related_subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class CreditsLedger(Base):
    """Immutable audit trail for every credit movement.

    Each row records a single atomic change to a bucket's balance. The full
    history of a user's credits can be reconstructed by replaying ledger entries
    in ``created_at`` order.

    V3-1 shadow mode: ledger entries are written alongside V2 quota transitions
    but do NOT affect job gating, billing, or entitlements.
    """

    __tablename__ = "credits_ledger"
    __table_args__ = (
        Index("idx_credits_ledger_user_id", "user_id"),
        Index("idx_credits_ledger_bucket_id", "bucket_id"),
        Index("idx_credits_ledger_direction", "direction"),
        Index("idx_credits_ledger_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("credits_buckets.id"), nullable=False
    )
    direction: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # "grant" | "reserve" | "capture" | "release" | "refund" | "rollback"
    credits_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    related_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    related_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    related_subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    reason_code: Mapped[str] = mapped_column(
        String(64), nullable=False, default="unspecified"
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class UserVoice(Base):
    """Per-user personal voice library entry.

    Originally MiniMax-only; Phase 4.1 added CosyVoice clone routing
    fields (``region_constraint`` / ``requires_worker`` / ``target_model`` /
    ``worker_provider`` / ``worker_region``) plus billing audit anchors
    (``clone_api_model`` / ``billing_sku`` / ``clone_provider_request_id`` /
    ``clone_worker_request_id``). See migration 030 + plan §User Voice
    Library Schema.
    """

    __tablename__ = "user_voices"
    __table_args__ = (
        Index("idx_user_voices_user_id", "user_id"),
        Index("idx_user_voices_source_hash_speaker_id", "user_id", "source_content_hash", "source_speaker_id"),
        Index("idx_user_voices_source_hash_speaker_name", "user_id", "source_content_hash", "source_speaker_name_key"),
        Index("idx_user_voices_source_ref", "user_id", "source_ref"),
        Index("idx_user_voices_phase4_region", "user_id", "region_constraint"),
        # Partial index — Codex 2026-05-25 三轮 finding：跳过 NULL 行
        # （大多数 MiniMax / VolcEngine 旧 row）。仅 CosyVoice clone 写入
        # ``clone_provider_request_id`` 才进索引。
        Index(
            "idx_user_voices_clone_provider_request_id",
            "clone_provider_request_id",
            postgresql_where=text("clone_provider_request_id IS NOT NULL"),
        ),
        # Phase 4.2 A.1（migration 031）partial index for 临时音色清理 sweeper.
        # 仅索引"是临时音色 + 尚未软删"的行；WHERE 条件与清理任务 SELECT
        # 完全对齐（plan v4-followup §12.3 / §12.4）。sweeper 成功删 DashScope
        # voice 后写 ``expired_at=now()`` → 该行从此 index 自动剔除（幂等）。
        Index(
            "idx_user_voices_temp_expires_pending",
            "temporary_expires_at",
            postgresql_where=text("is_temporary = TRUE AND expired_at IS NULL"),
        ),
        UniqueConstraint("user_id", "voice_id", name="uq_user_voices_user_voice"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    voice_id: Mapped[str] = mapped_column(String(200), nullable=False)
    voice_type: Mapped[str] = mapped_column(String(20), nullable=False, default="cloned")
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="minimax_voice_clone")
    tts_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(50), nullable=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    source_speaker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_content_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_upload_md5: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_video_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_speaker_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_speaker_name_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_content_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_content_era: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_content_tags: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    clone_sample_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    clone_sample_segment_ids: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    created_from: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Voice speed calibration (migration 013). Populated when the user
    # triggers POST /gateway/user-voices/{id}/calibrate-speed; NULL otherwise.
    chars_per_second: Mapped[float | None] = mapped_column(Float, nullable=True)
    chars_per_second_by_model: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    speed_calibrated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- Phase 4.1 (migration 030): CosyVoice worker routing + billing audit ---
    # Worker dispatch fields — server-side defaults so existing MiniMax /
    # VolcEngine rows backfill to "overseas_ok / requires_worker=False".
    region_constraint: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="overseas_ok"
    )
    requires_worker: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # TTS model lock — CosyVoice clone binds voice_id to one target_model
    # (flash / plus); cross-model TTS calls are rejected by tts_generator
    # guard (plan §Phase 4.1 §F Open Q #4).
    target_model: Mapped[str | None] = mapped_column(String(50), nullable=True)
    worker_provider: Mapped[str | None] = mapped_column(String(30), nullable=True)
    worker_region: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # Clone API model vs accounting SKU split (Codex 2026-05-24 P0):
    # ``clone_api_model="voice-enrollment"`` is the SDK param; ``billing_sku``
    # is the Alibaba billing line item, **nullable until first real bill
    # confirms** (Codex 2026-05-25 finding — voice enrollment may not
    # produce its own bill line at all).
    clone_api_model: Mapped[str | None] = mapped_column(String(50), nullable=True)
    billing_sku: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Audit anchors. ``clone_worker_request_id`` is the worker-side UUID
    # required for every audit trail. ``clone_provider_request_id`` is the
    # DashScope SDK request id — kept for support traceability but **not**
    # used to join Alibaba billing (the CSV doesn't expose this field).
    clone_provider_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    clone_worker_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- Phase 4.2 A.1 (migration 031): CosyVoice 临时音色生命周期 ---
    # ``is_temporary``：用户克隆时未勾"保存到我的音色库"则 TRUE（默认行为）。
    # 旧 row（Phase 4.1 之前所有 voice）默认 FALSE，行为不变。
    is_temporary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # ``temporary_expires_at``：临时音色计划过期时间（仅 ``is_temporary=TRUE``
    # 才填，默认 ``now()+7d``，应用层填）。
    #
    # ⚠️ **绝不**改名为 ``expires_at`` —— 同表 ``expired_at`` 已是软删时间戳
    # （migration 010 落，本表 active 判据是 ``WHERE expired_at IS NULL``）。
    # 字面太近 → query 大概率写错。Codex 2026-05-26 v4-followup review 重点。
    #
    # ⚠️ **绝不**用本字段判断 active —— 它只是清理 sweeper 的入选条件之一。
    # active 仍以现有 ``expired_at IS NULL`` 为唯一标准。
    #
    # 清理 sweeper 选行 SQL（与 partial index ``idx_user_voices_temp_expires_pending``
    # 完全对齐）：
    #     WHERE provider = 'cosyvoice_voice_clone'
    #       AND is_temporary = TRUE
    #       AND temporary_expires_at < now()
    #       AND expired_at IS NULL
    temporary_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    # ── Phase 4.3b 临时音色清理 sweeper 追踪字段（migration 033）──────────────
    # 仅对 ``provider='cosyvoice_voice_clone' AND is_temporary=TRUE`` 的到期行有意义；
    # 其它 row 全默认（attempts=0 / 其余 NULL），行为不变。清理 = 删 DashScope
    # voice（付费）→ 软删 DB（写 ``expired_at``）。失败永不写 ``expired_at``，靠
    # 下面字段做 backoff + give-up + 并发 claim-lease。详见
    # docs/plans/2026-05-28-phase43b-temporary-voice-cleanup-sweeper-spec.md。
    #
    # backoff / give-up：
    cleanup_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cleanup_retry_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cleanup_last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 并发 claim-lease（spec §2.7）：认领时写 ``claim_until=now()+LEASE`` +
    # ``run_id``；select 过滤 ``claim_until IS NULL OR claim_until < now()``；
    # 完成更新用 ``run_id`` 守卫防 lease 被重认领后 clobber。FOR UPDATE SKIP
    # LOCKED 是 PG 语义（并发原子性留 PG integration 测试）。
    cleanup_claim_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cleanup_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class ExpressCloneReservation(Base):
    """Phase 4.3a PR2 — Express auto-clone atomic reservation（migration 032）.

    Express 快捷版自动克隆的**原子成本闸**载体。付费 worker clone 之前，
    pipeline 先在此表原子预占一个名额；register 成功 consume，失败 release,
    TTL 到期 expired。独立于 ``user_voices`` 音色事实表（spec §2.1），避免
    污染 list/match/routing/count 语义。

    状态机：``reserved → consumed | released | expired``（spec §3）。

    并发原子性靠 reserve service 在 transaction 内锁 ``users`` row
    （``SELECT ... FOR UPDATE``）串行化同一 user 的 reserve；
    ``uq_express_reservation_active`` partial unique 是幂等第二道防线。
    """

    __tablename__ = "express_clone_reservations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    speaker_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # 状态机；server_default 'reserved' 与 migration 一致
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'reserved'")
    )
    target_model: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    # TTL：reserve 时应用层填 now + RESERVATION_TTL。NOT NULL 强制每个
    # reservation 都有过期时间（spec §4.1：永不过期是 bug）。
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # consume 时填（关联 user_voices.voice_id）
    consumed_voice_id: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    # release / expire 原因（审计）
    released_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

    __table_args__ = (
        # 幂等第二道防线（spec §2.3 / §4.1）：同 (user,job,speaker) 最多
        # 一个 active(reserved)。partial unique —— consumed/released/expired
        # 不占唯一槽。
        # partial unique：同时声明 postgresql_where + sqlite_where（同条件），
        # 让 partial 语义在 PG（生产）和 sqlite（单测）都生效。否则 sqlite
        # 忽略 postgresql_where → 退化成全量 unique → expired 行仍占 (user,
        # job,speaker) 唯一槽，新 reserved 插入误冲突（生产 PG partial 正确）。
        Index(
            "uq_express_reservation_active",
            "user_id",
            "job_id",
            "speaker_id",
            unique=True,
            postgresql_where=text("status = 'reserved'"),
            sqlite_where=text("status = 'reserved'"),
        ),
        # budget count 查询（spec §5）
        Index(
            "idx_express_reservation_user_status",
            "user_id",
            "status",
            "created_at",
        ),
        # TTL sweeper + reserve 内 inline expire 选行（spec §4.1 step 2 + §8）
        Index(
            "idx_express_reservation_ttl_pending",
            "expires_at",
            postgresql_where=text("status = 'reserved'"),
            sqlite_where=text("status = 'reserved'"),
        ),
    )


class FreeServiceDailyUsage(Base):
    """Phase 2a free tier — per-job daily ledger for the 1/day free-service cap
    (migration 034). **Independent** of ``users.free_jobs_quota_*`` (that's the
    free *plan* total, not per-day) — free-service jobs do NOT touch it.

    Daily cap = active(reserved|consumed) rows for ``(user_id, usage_date)`` where
    ``usage_date`` is the **Asia/Shanghai** natural day. State machine
    ``reserved → consumed | released | expired`` mirrors ``ExpressCloneReservation``;
    ``create_idempotency_key`` is the idempotency dimension (partial-unique while
    reserved). Indexes live in migration 034.
    """

    __tablename__ = "free_service_daily_usage"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Asia/Shanghai natural day "YYYY-MM-DD" (free_service_quota.shanghai_day_key)
    usage_date: Mapped[str] = mapped_column(String(10), nullable=False)
    create_idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'reserved'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    # TTL: reserve fills now + RESERVE_TTL_MINUTES. NOT NULL — a never-expiring
    # reserved row would block the daily slot forever after a crashed create.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    released_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PricingConfigVersion(Base):
    """Versioned pricing configuration for admin publish/draft/archive workflow."""

    __tablename__ = "pricing_config_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # "active" | "draft" | "archived"
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_pricing_config_versions_status", "status"),
        Index("ix_pricing_config_versions_version", "version"),
        Index("ix_pricing_config_versions_created_at", "created_at"),
        # P1-11c (audit 2026-05-07, D-HIGH-3): pricing_admin computes
        # next version as MAX(version) + 1 then INSERTs without a row
        # lock — two concurrent admins both see max=N, both insert N+1,
        # leaving duplicate version rows. Migration 017 adds this UNIQUE
        # so the second insert fails and the caller can retry.
        UniqueConstraint(
            "version", name="uq_pricing_config_versions_version",
        ),
        # P1-11c follow-up² (Codex review 6019beb): even with the
        # version UNIQUE above, two publish requests can interleave at
        # PostgreSQL READ COMMITTED such that they archive different
        # rows and INSERT distinct versions, leaving multiple
        # status='active' rows. Partial UNIQUE INDEX on status WHERE
        # status='active' enforces single-active at the schema level;
        # second INSERT fails with IntegrityError and pricing_admin's
        # existing handler maps that to HTTP 409. See migration 018
        # for the full race walkthrough.
        Index(
            "uq_pricing_config_versions_active_status",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        # P2-24 / D-HIGH-2 (audit 2026-05-07, migration 021):
        # ``auth.create_session`` runs ``DELETE FROM sessions WHERE
        # expires_at <= NOW()`` on every login. Without this index the
        # delete is a sequential scan, which becomes a per-login
        # latency cliff after the table accumulates 10k+ rows.
        Index("idx_sessions_expires_at", "expires_at"),
    )

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


# ---------------------------------------------------------------------------
# AI customer support + notification system (migration 020, 2026-05-08)
# Plan: docs/plans/2026-05-08-ai-customer-support-handoff-plan.md
# ---------------------------------------------------------------------------


class SupportConversation(Base):
    """Top-level support conversation between user/visitor and AI/human agent.

    ``user_id`` nullable so anonymous (pre-login) visitors can still chat;
    those rows carry an ``anonymous_id`` instead.

    ``handoff_state`` evolves independently of ``status``: a conversation may
    be closed (``status=closed``) with no handoff ever happening, or sit at
    ``status=waiting_human`` after handoff_state moves to ``created``.
    """

    __tablename__ = "support_conversations"
    __table_args__ = (
        Index("idx_support_conversations_user_id", "user_id"),
        Index("idx_support_conversations_anonymous_id", "anonymous_id"),
        Index("idx_support_conversations_status", "status"),
        Index("idx_support_conversations_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    anonymous_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channel: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="web"
    )  # "web" | "wechat" | "email"
    entrypoint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    page_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="open"
    )  # "open" | "waiting_human" | "handled" | "closed"
    handoff_state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="none"
    )  # "none" | "recommended" | "requested" | "created" | "failed" | "closed"
    handoff_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    handoff_provider_conversation_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    notification_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    last_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class SupportMessage(Base):
    """Single message inside a support conversation.

    ``redacted_body`` is the version safe to show a human agent (PII pre-
    redacted). ``body`` is the original; AI-prompt-bound paths read from
    ``redacted_body`` whenever it is non-NULL.
    """

    __tablename__ = "support_messages"
    __table_args__ = (
        Index("idx_support_messages_conversation_id", "conversation_id"),
        Index("idx_support_messages_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("support_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    sender: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # "user" | "assistant" | "human" | "system"
    body: Mapped[str] = mapped_column(Text, nullable=False)
    redacted_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class SupportHandoffRequest(Base):
    """One row per attempt to escalate a conversation to a human channel."""

    __tablename__ = "support_handoff_requests"
    __table_args__ = (
        Index("idx_support_handoff_requests_conversation_id", "conversation_id"),
        Index("idx_support_handoff_requests_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("support_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # "email" | "chatwoot" | "wechat_kf"
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )  # "pending" | "created" | "failed" | "closed"
    provider_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class SupportAIUsage(Base):
    """Ledger of every routing decision, written even for template/FAQ paths.

    For ``route ∈ {template, faq}`` the cost columns stay 0; for
    ``route == "llm"`` they reflect the budget accumulator's view of cost.
    Drives the monthly budget guard via ``budget_month`` (YYYY-MM string).
    """

    __tablename__ = "support_ai_usage"
    __table_args__ = (
        Index("idx_support_ai_usage_budget_month", "budget_month"),
        Index("idx_support_ai_usage_conversation_id", "conversation_id"),
        Index("idx_support_ai_usage_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("support_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    anonymous_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    input_usd_per_1m_tokens: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0"
    )
    output_usd_per_1m_tokens: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0"
    )
    estimated_cost_usd: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0"
    )
    budget_month: Mapped[str] = mapped_column(String(7), nullable=False)  # "2026-05"
    route: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # "template" | "faq" | "llm" | "handoff"
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class SystemAnnouncement(Base):
    """Admin-composed broadcast notification (migration 023, 2026-05-08).

    Plan §16.7 P2 elevation: admin picks an audience predicate, system
    fans out one ``user_notifications`` row per matched user. The
    announcement row is the source-of-truth for "what was sent" and
    enables clone-and-resend ("edit a previous announcement").

    ``audience_kind`` values supported by the resolver (P1):
      - all
      - registered_within_days  (params: {"days": int})
      - plan_free / plan_plus / plan_pro / plan_paid
      - trial_active
      - trial_ending_within_days  (params: {"days": int})
      - trial_ended_within_days   (params: {"days": int})
      - paid_no_jobs
      - inactive_for_days  (params: {"days": int})
      - active_with_jobs_within_days  (params: {"days": int, "min_jobs": int})
      - had_failures_within_days  (params: {"days": int})
      - admin_only
    """

    __tablename__ = "system_announcements"
    __table_args__ = (
        Index("idx_system_announcements_status", "status"),
        Index("idx_system_announcements_created_at", "created_at"),
        Index("idx_system_announcements_admin_id", "created_by_admin_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    topic: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="maintenance"
    )
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="info"
    )
    action_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    audience_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="all"
    )
    audience_params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_announcements.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="draft"
    )  # "draft" | "sent" | "archived"
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recipient_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # When True, every fanned-out user_notifications row carries
    # popup=true so the recipient sees a modal on next page load
    # instead of (only) a quiet bell entry. Migration 024.
    popup: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class SupportAdminPresence(Base):
    """Per-admin heartbeat presence (migration 022, 2026-05-08).

    One row per admin user_id. Updated on each heartbeat ping (default
    30s from frontend AppShell). Routing logic checks
    ``status == "online" AND last_heartbeat_at > now - threshold``.

    Independent from ``sessions``: a 30-day session means "auth is
    valid", a fresh heartbeat means "admin is at the keyboard". A
    ticket only routes to in-product chat when both hold.
    """

    __tablename__ = "support_admin_presence"
    __table_args__ = (
        Index(
            "idx_support_admin_presence_status_heartbeat",
            "status",
            "last_heartbeat_at",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="online"
    )  # "online" | "paused" | "offline"
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class UserNotification(Base):
    """User-visible projection of pipeline events.

    NOT a replacement for ``events.jsonl`` (which is internal/observability
    only). This is the sanitized message-stream the UI reads. Admin alerts
    do NOT live here — those go to email/webhook directly.

    ``dedupe_key`` + the partial unique index (created in migration 020) lets
    P2 add throttling without a schema change. P1 leaves the column NULL for
    every notification, so dedupe is a no-op.
    """

    __tablename__ = "user_notifications"
    __table_args__ = (
        Index("idx_user_notifications_user_id_created_at", "user_id", "created_at"),
        Index("idx_user_notifications_user_id_unread", "user_id", "read_at"),
        Index("idx_user_notifications_job_id", "job_id"),
        Index("idx_user_notifications_scope", "scope"),
        Index(
            "uq_user_notifications_user_job_dedupe",
            "user_id",
            "job_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # "system" | "user" | "job"
    topic: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # "billing" | "account" | "artifact" | "support" | "maintenance"
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="info"
    )  # "info" | "success" | "warning" | "error"
    related_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    related_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    artifact_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # ``popup``: when True, the frontend renders this notification as
    # a modal on next page load (instead of only a quiet bell entry).
    # Set at fan-out time from the source announcement's flag.
    # Migration 024 adds a partial index over (user_id, created_at)
    # filtered to popup=true AND not-yet-dismissed for fast lookup.
    popup: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # ``popup_dismissed_at``: separate from ``read_at`` so closing the
    # modal doesn't silently mark the underlying notification as read.
    # The bell badge stays unread until the user explicitly reads.
    popup_dismissed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Pan Backup — admin network-disk backup feature (migration 029, 2026-05-14)
# Plan: docs/plans/2026-05-14-admin-pan-backup-implementation-plan.md T1.2
# ---------------------------------------------------------------------------


class PanCredentials(Base):
    __tablename__ = "pan_credentials"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    access_token_encrypted: Mapped[bytes] = mapped_column(nullable=False)
    refresh_token_encrypted: Mapped[bytes] = mapped_column(nullable=False)
    access_token_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    scope: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="active",
    )  # 'active' | 'revoked'
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()"),
    )
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_pan_credentials_user_provider"),
    )


class BackupRecord(Base):
    __tablename__ = "backup_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # NOT FK to jobs.job_id — backup row must survive jobs row deletion.
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_edit_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    remote_path: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    md5: Mapped[str] = mapped_column(String(32), nullable=False)
    manifest_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # uploading | uploaded | failed | restoring | restored | deleted
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class PanOauthState(Base):
    __tablename__ = "pan_oauth_states"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


# ---------------------------------------------------------------------------
# APF P0 anonymous preview tables (migration 035, 2026-06-10)
# ---------------------------------------------------------------------------


class AnonymousPreviewDailyUsage(Base):
    """Per-(scope, scope_key, mode, usage_date) rate-limit counter.

    scope ∈ 'global' | 'ip' | 'device' | 'source'. scope_key holds the
    HMAC-SHA256 hex of the raw value — no raw IP / device / source text is
    persisted. The unique index ``uq_anon_preview_daily_usage`` is the
    ON CONFLICT target for PgRateLimitCounterStore's atomic upsert.
    """

    __tablename__ = "anonymous_preview_daily_usage"
    __table_args__ = (
        Index(
            "uq_anon_preview_daily_usage",
            "scope",
            "scope_key",
            "mode",
            "usage_date",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="free"
    )
    usage_date: Mapped[str] = mapped_column(String(10), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class AnonymousSession(Base):
    """Anonymous visitor session identified by HMAC token hash.

    claim_user_id is left NULL until Phase 4 when a user registers after
    a preview and claims their anonymous session.
    """

    __tablename__ = "anonymous_sessions"
    __table_args__ = (
        Index("ix_anonymous_sessions_expires_at", "expires_at"),
    )

    session_id_hash: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Phase 4 留行
    claim_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )


class AnonymousPreviewRecord(Base):
    """Status-only intake record for an anonymous preview request.

    Column names mirror src.services.anonymous_preview_intake.PreviewRecord
    field names. audit is JSONB (matching project-wide JSONB convention).
    job_id is a string FK to jobs.job_id (not UUID) — nullable until a
    preview job is actually spawned (APF Phase 3+).
    """

    __tablename__ = "anonymous_preview_records"
    __table_args__ = (
        Index("ix_anon_preview_records_session_id", "session_id"),
        Index("ix_anon_preview_records_expires_at", "expires_at"),
    )

    preview_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    status_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="free"
    )
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Phase 4 留行
    claim_token_placeholder: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    audit: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
