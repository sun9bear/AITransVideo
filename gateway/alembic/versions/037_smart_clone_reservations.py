"""P3a: Smart 预览克隆 600 点预扣 reservation + durable billing event.

Revision ID: 037_smart_clone_reservations
Revises: 036_job_language_fields
Create Date: 2026-06-14

plan 2026-06-14-p3-smart-clone-600-credit-subplan v3。智能版 3 分钟预览克隆
（MiniMax 主说话人）的**钱-正确性账本**，CodeX 复审硬要求：钱的事实只依赖
DB（reservation + billing event），不依赖文件产物。

两张表：

1. ``smart_clone_reservations`` —— 600 点预扣 reservation 状态机
   （``reserved → captured | released | expired``）。gateway 建预览任务时创建；
   terminal finalizer 按 billing event 幂等 capture/release。镜像
   ``express_clone_reservations``（migration 032）的并发/TTL/partial-unique 模式。
   - partial unique ``(task_id, purpose)`` where status='reserved'：同一任务
     最多一个 active reservation（幂等第二道防线）。
   - TTL ``expires_at``：非终态卡死不永久占点/占库容（finalizer/sweeper 回收）。

2. ``clone_billing_events`` —— **唯一权威计费信号**（CodeX P0-1）。pipeline 在
   MiniMax 返回 voice_id 瞬间，经 register-smart endpoint 同事务写入。
   - unique ``reservation_id``：一个 reservation 最多一条 event（幂等，防重复写/
     重复 capture）。

本 migration 只做 schema；service / endpoint / finalizer / pipeline gate 在后续
PR。默认 OFF（``smart_preview_clone_enabled`` admin 旋钮控制是否创建 reservation）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "037_smart_clone_reservations"
down_revision: Union[str, None] = "036_job_language_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- smart_clone_reservations ---
    op.create_table(
        "smart_clone_reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # 智能版预览任务 id（preview job）
        sa.Column("task_id", sa.String(length=64), nullable=False),
        # 预扣用途（唯一约束维度之一；当前固定 'smart_clone_minimax_600'）
        sa.Column(
            "purpose",
            sa.String(length=40),
            nullable=False,
            server_default=sa.text("'smart_clone_minimax_600'"),
        ),
        # 预扣点数（600；落表防 runtime pricing 漂移导致 capture/reserve 不一致）
        sa.Column("amount_credits", sa.Integer(), nullable=False),
        # 状态机：reserved → captured | released | expired
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'reserved'"),
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
        # TTL：reserve 时应用层填 now + RESERVATION_TTL；NOT NULL（永不过期是 bug）
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # capture/release 落定时间（审计）
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        # capture 时关联的 user_voices.voice_id（仅审计；真账本是 billing event）
        sa.Column("captured_voice_id", sa.String(length=200), nullable=True),
        # capture/release 原因（审计：insufficient_credits / voice_library_full /
        # clone_disabled / clone_failed / not_triggered / ttl_expired / captured ...）
        sa.Column("reason_code", sa.String(length=64), nullable=True),
    )

    # 幂等第二道防线：同 (task_id, purpose) 最多一个 active(reserved)。
    # partial unique（PG）—— captured/released/expired 不占唯一槽。
    op.create_index(
        "uq_smart_clone_reservation_active",
        "smart_clone_reservations",
        ["task_id", "purpose"],
        unique=True,
        postgresql_where=sa.text("status = 'reserved'"),
    )
    # 库容/budget count 查询：user + status + created_at
    op.create_index(
        "idx_smart_clone_reservation_user_status",
        "smart_clone_reservations",
        ["user_id", "status", "created_at"],
    )
    # TTL sweeper + reserve 内 inline expire 选行：只索引 reserved 行
    op.create_index(
        "idx_smart_clone_reservation_ttl_pending",
        "smart_clone_reservations",
        ["expires_at"],
        postgresql_where=sa.text("status = 'reserved'"),
    )

    # --- clone_billing_events（唯一权威计费信号，CodeX P0-1）---
    op.create_table(
        "clone_billing_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column(
            "reservation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("smart_clone_reservations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("voice_id", sa.String(length=200), nullable=False),
        # 是否计费（本任务真新建付费克隆 = true；复用/缓存/fallback 不写本表）
        sa.Column(
            "chargeable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # 幂等：一个 reservation 最多一条 billing event（防重复写 → 防重复 capture）
        sa.UniqueConstraint("reservation_id", name="uq_clone_billing_event_reservation"),
    )
    op.create_index(
        "idx_clone_billing_event_task",
        "clone_billing_events",
        ["task_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_clone_billing_event_task", table_name="clone_billing_events")
    op.drop_table("clone_billing_events")
    op.drop_index(
        "idx_smart_clone_reservation_ttl_pending",
        table_name="smart_clone_reservations",
    )
    op.drop_index(
        "idx_smart_clone_reservation_user_status",
        table_name="smart_clone_reservations",
    )
    op.drop_index(
        "uq_smart_clone_reservation_active",
        table_name="smart_clone_reservations",
    )
    op.drop_table("smart_clone_reservations")
