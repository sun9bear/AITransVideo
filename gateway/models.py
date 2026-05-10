"""SQLAlchemy models for users, sessions, and jobs."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
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
    )  # "paid" | "failed" | "refunded"
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
    """Per-user personal voice library entry (MiniMax cloned voices)."""

    __tablename__ = "user_voices"
    __table_args__ = (
        Index("idx_user_voices_user_id", "user_id"),
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
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Voice speed calibration (migration 013). Populated when the user
    # triggers POST /gateway/user-voices/{id}/calibrate-speed; NULL otherwise.
    chars_per_second: Mapped[float | None] = mapped_column(Float, nullable=True)
    chars_per_second_by_model: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    speed_calibrated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


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
