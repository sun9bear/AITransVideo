"""APF P0 — anonymous preview schema tables + jobs column.

Revision ID: 035_anonymous_preview
Revises: 034_free_service_daily_usage
Create Date: 2026-06-10

新增三张表和一个 jobs 列以支持匿名预览漏斗（APF）P0 垂直切片。

三表
----
1. ``anonymous_preview_daily_usage``
   照 034 ``free_service_daily_usage`` 计数 ledger 形态，但不做
   state-machine — 只存每日计数。唯一键 (scope, scope_key, mode,
   usage_date)；scope ∈ global/ip/device/source；scope_key 存
   HMAC-SHA256 hex（**不含 raw IP / raw device / raw source 明文**）；
   mode 默认 'free'。原子 INSERT … ON CONFLICT DO UPDATE 语义由
   gateway/anonymous_preview_quota.py 的 PgRateLimitCounterStore
   负责；migration 只建 schema。

2. ``anonymous_sessions``
   token hash（session_id_hash）PK；TTL 由 expires_at 控制；
   claim_user_id 可空（Phase 4 用户认领留行）。

3. ``anonymous_preview_records``
   对应 src.services.anonymous_preview_intake.PreviewRecord 契约
   字段，JSONB audit 列用 PG 方言 JSONB（照 models.py 惯例）。
   preview_id PK；session_id 索引（非 FK，因 session 可能超期删除）；
   job_id 可空（关联 jobs.job_id，非 UUID FK，照 Job model 字段类型）；
   claim_token_placeholder 可空（Phase 4 留行）。

jobs 列
-------
``is_anonymous_preview`` boolean NOT NULL DEFAULT false，带
partial index ``ix_jobs_anon_preview_status ON jobs(status)
WHERE is_anonymous_preview``。

sentinel 系统用户
-----------------
email ``anonymous-preview@system``：幂等插入（ON CONFLICT DO NOTHING），
密码哈希给随机 CRYPT-like 占位符（不可登录）；downgrade 删除。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "035_anonymous_preview"
down_revision: Union[str, None] = "034_free_service_daily_usage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SENTINEL_EMAIL = "anonymous-preview@system"
# bcrypt-shaped placeholder — random bytes ensure no real login possible
_SENTINEL_PW_HASH = (
    "$2b$12$invalid_placeholder_anonymous_preview_system_not_loginable"
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. anonymous_preview_daily_usage
    # ------------------------------------------------------------------
    op.create_table(
        "anonymous_preview_daily_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # scope ∈ 'global' | 'ip' | 'device' | 'source'
        sa.Column("scope", sa.String(length=16), nullable=False),
        # HMAC-SHA256 hex of the raw value — no raw IP / device / source
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        # mode ∈ 'free' (default); reserved for future 'trial' / 'paid'
        sa.Column(
            "mode",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'free'"),
        ),
        # ISO-8601 date string (Asia/Shanghai) e.g. '2026-06-10'
        sa.Column("usage_date", sa.String(length=10), nullable=False),
        # current count for this (scope, scope_key, mode, usage_date)
        sa.Column(
            "count",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # Unique composite key — the ON CONFLICT target for atomic upsert
    op.create_index(
        "uq_anon_preview_daily_usage",
        "anonymous_preview_daily_usage",
        ["scope", "scope_key", "mode", "usage_date"],
        unique=True,
    )

    # ------------------------------------------------------------------
    # 2. anonymous_sessions
    # ------------------------------------------------------------------
    op.create_table(
        "anonymous_sessions",
        # session_id_hash = HMAC-SHA256 of raw session token
        sa.Column("session_id_hash", sa.String(length=128), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # Phase 4 留行：user claims their anonymous session post-signup
        sa.Column("claim_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    op.create_index(
        "ix_anonymous_sessions_expires_at",
        "anonymous_sessions",
        ["expires_at"],
    )

    # ------------------------------------------------------------------
    # 3. anonymous_preview_records
    # ------------------------------------------------------------------
    op.create_table(
        "anonymous_preview_records",
        # PK: string preview id (e.g. "prv_<hash12>")
        sa.Column("preview_id", sa.String(length=64), primary_key=True),
        # session_id_hash — indexed but not FK (sessions may expire/delete)
        sa.Column("session_id", sa.String(length=128), nullable=False),
        # PreviewStatus enum value
        sa.Column("status", sa.String(length=32), nullable=False),
        # PreviewRecord.status_reason
        sa.Column("status_reason", sa.String(length=256), nullable=True),
        # SourceType enum value
        sa.Column("source_type", sa.String(length=32), nullable=False),
        # HMAC-SHA256 of raw source content
        sa.Column("source_hash", sa.String(length=128), nullable=False),
        # 'free' | 'trial' | reserved
        sa.Column(
            "mode",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'free'"),
        ),
        # Linked job_id (string, NOT UUID FK) — nullable until job spawned
        sa.Column("job_id", sa.String(length=64), nullable=True),
        # Phase 4 留行：pre-minted claim token placeholder
        sa.Column("claim_token_placeholder", sa.String(length=256), nullable=True),
        # Compliance / intake audit blob (JSONB, matching models.py convention)
        sa.Column("audit", postgresql.JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_index(
        "ix_anon_preview_records_session_id",
        "anonymous_preview_records",
        ["session_id"],
    )

    op.create_index(
        "ix_anon_preview_records_expires_at",
        "anonymous_preview_records",
        ["expires_at"],
    )

    # ------------------------------------------------------------------
    # 4. jobs.is_anonymous_preview column + partial index
    # ------------------------------------------------------------------
    op.add_column(
        "jobs",
        sa.Column(
            "is_anonymous_preview",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # ix_jobs_anon_preview_status is a partial index on the EXISTING, hot
    # `jobs` table. A plain CREATE INDEX takes an ACCESS EXCLUSIVE lock and
    # blocks ALL reads/writes to `jobs` for the build duration — unacceptable
    # on a payment-active prod DB. CONCURRENTLY avoids the lock but cannot run
    # inside a transaction, so we step outside the migration transaction via
    # autocommit_block(). The add_column above is committed first (entering the
    # block commits the current tx), so the partial predicate resolves.
    # IF NOT EXISTS keeps re-runs idempotent. (The three new tables' indexes
    # stay plain/transactional — they're built on empty tables, no lock risk.)
    with op.get_context().autocommit_block():
        # First drop any INVALID leftover from a previously cancelled /
        # interrupted CREATE INDEX CONCURRENTLY. Without this, the
        # if_not_exists below would see the dead-name slot and let alembic
        # mark this revision applied while the index is unusable (mirrors the
        # migration 021 pattern).
        conn = op.get_bind()
        invalid_row = conn.execute(
            sa.text(
                "SELECT 1 FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :idx_name AND i.indisvalid = false"
            ),
            {"idx_name": "ix_jobs_anon_preview_status"},
        ).first()
        if invalid_row is not None:
            op.execute(
                "DROP INDEX CONCURRENTLY IF EXISTS ix_jobs_anon_preview_status"
            )

        op.create_index(
            "ix_jobs_anon_preview_status",
            "jobs",
            ["status"],
            postgresql_where=sa.text("is_anonymous_preview"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )

    # ------------------------------------------------------------------
    # 5. Sentinel system user — idempotent (ON CONFLICT DO NOTHING)
    # ------------------------------------------------------------------
    op.execute(
        sa.text(
            """
            INSERT INTO users (id, email, display_name, password_hash,
                               is_active, role, plan_code,
                               free_jobs_quota_total, free_jobs_quota_used,
                               created_at, updated_at)
            VALUES (
                gen_random_uuid(),
                :email,
                'Anonymous Preview System',
                :pw_hash,
                false,
                'user',
                'free',
                0,
                0,
                now(),
                now()
            )
            ON CONFLICT (email) DO NOTHING
            """
        ).bindparams(email=_SENTINEL_EMAIL, pw_hash=_SENTINEL_PW_HASH)
    )


def downgrade() -> None:
    # ------------------------------------------------------------------
    # Reverse order
    # ------------------------------------------------------------------

    # 5. Remove sentinel
    op.execute(
        sa.text("DELETE FROM users WHERE email = :email").bindparams(
            email=_SENTINEL_EMAIL
        )
    )

    # 4. jobs column + partial index. DROP INDEX CONCURRENTLY (also outside a
    # transaction) so downgrade never takes the ACCESS EXCLUSIVE lock either.
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_jobs_anon_preview_status",
            table_name="jobs",
            postgresql_concurrently=True,
            if_exists=True,
        )
    op.drop_column("jobs", "is_anonymous_preview")

    # 3. anonymous_preview_records
    op.drop_index("ix_anon_preview_records_expires_at", table_name="anonymous_preview_records")
    op.drop_index("ix_anon_preview_records_session_id", table_name="anonymous_preview_records")
    op.drop_table("anonymous_preview_records")

    # 2. anonymous_sessions
    op.drop_index("ix_anonymous_sessions_expires_at", table_name="anonymous_sessions")
    op.drop_table("anonymous_sessions")

    # 1. anonymous_preview_daily_usage
    op.drop_index("uq_anon_preview_daily_usage", table_name="anonymous_preview_daily_usage")
    op.drop_table("anonymous_preview_daily_usage")
