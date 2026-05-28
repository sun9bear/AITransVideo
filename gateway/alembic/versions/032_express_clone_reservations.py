"""Phase 4.3a PR2-A: Express auto-clone atomic reservation table.

Revision ID: 032_express_clone_reservations
Revises: 031_user_voice_temp_expiry
Create Date: 2026-05-28

Phase 4.3a PR2 spec §3 落实。新建独立 ``express_clone_reservations`` 表，
承载 Express 快捷版自动克隆的**原子成本闸**（atomic reservation）。

为什么独立表而不复用 ``user_voices`` placeholder（spec §2.1 决策 1）：
- ``user_voices`` 是音色事实表（list / match / routing / count 都查它），
  PR1-D1 刚把 ``is_temporary`` 隔离干净；再塞 reservation placeholder 又
  污染所有这些查询语义。
- 独立表状态机 ``reserved → consumed | released | expired`` 清晰，与音色
  事实表解耦。

字段（spec §3）：
- ``status``：状态机；reserve 时 'reserved'，register 成功 consume,
  失败 release，TTL 到期 expired
- ``target_model``：预占时记录（cosyvoice-v3.5-flash）
- ``expires_at``：TTL（reserve 时 now + RESERVATION_TTL，默认 30min）
- ``consumed_voice_id``：consume 时关联 user_voices.voice_id
- ``released_reason``：release / expire 原因（审计）

三个索引（spec §3）：
1. ``uq_express_reservation_active``（partial UNIQUE）：同 (user,job,speaker)
   最多一个 active(reserved)。这是 reserve 幂等的**第二道防线**（spec §2.3 +
   §4.1）——即使 users-row-lock 串行化有 bug，DB 唯一约束也挡住第二个
   reserved 行。sqlite 也支持 partial unique，单测可验。
2. ``idx_express_reservation_user_status``：budget count 查询
   （user + status + created_at；spec §5）
3. ``idx_express_reservation_ttl_pending``（partial）：TTL sweeper +
   reserve 内 inline expire 选行（status='reserved' + expires_at < now;
   spec §4.1 step 2 + §8）

PR2-A 只做 schema + ORM + 守卫；service / endpoint / pipeline 不动。
本 migration 暂不部署 US prod —— 待 Phase 4.3a 部署阶段统一上线。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "032_express_clone_reservations"
down_revision: Union[str, None] = "031_user_voice_temp_expiry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "express_clone_reservations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("speaker_id", sa.String(length=64), nullable=False),
        # 状态机：reserved → consumed | released | expired
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'reserved'"),
        ),
        sa.Column("target_model", sa.String(length=50), nullable=False),
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
        # TTL：reserve 时应用层填 now + RESERVATION_TTL；NOT NULL 强制每个
        # reservation 都有过期时间（spec §4.1：临时音色永不过期是 bug）
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # consume 时填（关联 user_voices.voice_id）
        sa.Column("consumed_voice_id", sa.String(length=200), nullable=True),
        # release / expire 原因（审计：register_failed / upload_failed /
        # ttl_expired / unexpected_error ...）
        sa.Column("released_reason", sa.String(length=64), nullable=True),
    )

    # 1. 幂等第二道防线（spec §2.3 / §4.1）：同 (user,job,speaker) 最多
    #    一个 active(reserved)。partial unique —— 已 consumed/released/expired
    #    的不占唯一槽，所以同 (user,job,speaker) 可以有历史多条非 active 行。
    op.create_index(
        "uq_express_reservation_active",
        "express_clone_reservations",
        ["user_id", "job_id", "speaker_id"],
        unique=True,
        postgresql_where=sa.text("status = 'reserved'"),
    )

    # 2. budget count 查询（spec §5）：按 user + status + created_at 过滤
    #    （daily_count 要 created_at >= today_start；active count 按 status）
    op.create_index(
        "idx_express_reservation_user_status",
        "express_clone_reservations",
        ["user_id", "status", "created_at"],
    )

    # 3. TTL sweeper + reserve 内 inline expire 选行（spec §4.1 step 2 + §8）：
    #    只索引 status='reserved' 行，按 expires_at。partial 让 sweeper /
    #    inline-expire 扫描 cost 与全表大小解耦。
    op.create_index(
        "idx_express_reservation_ttl_pending",
        "express_clone_reservations",
        ["expires_at"],
        postgresql_where=sa.text("status = 'reserved'"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_express_reservation_ttl_pending",
        table_name="express_clone_reservations",
    )
    op.drop_index(
        "idx_express_reservation_user_status",
        table_name="express_clone_reservations",
    )
    op.drop_index(
        "uq_express_reservation_active",
        table_name="express_clone_reservations",
    )
    op.drop_table("express_clone_reservations")
