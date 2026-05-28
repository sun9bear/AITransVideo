"""Phase 4.3b-A: user_voices cleanup tracking columns (temporary-voice sweeper).

Revision ID: 033_user_voice_cleanup_tracking
Revises: 032_express_clone_reservations
Create Date: 2026-05-28

Phase 4.3b spec §3 落实。给 ``user_voices`` 加 5 列，承载临时音色清理 sweeper
（删 DashScope voice + 软删 DB）的 backoff / give-up / 并发 claim-lease 状态。
**不新表 / 不新索引** —— 复用现有 ``expired_at`` 软删 + 部分索引
``idx_user_voices_temp_expires_pending``（``WHERE is_temporary=TRUE AND
expired_at IS NULL``）覆盖 sweeper 主筛选；新列均为 filter。

5 列（spec §3）：
- ``cleanup_attempts``    INT NOT NULL DEFAULT 0   失败重试计数（>=MAX → give-up）
- ``cleanup_retry_after`` TIMESTAMPTZ NULL         backoff：下次可重试时刻
- ``cleanup_last_error``  VARCHAR(200) NULL        最近一次失败 code（审计）
- ``cleanup_claim_until`` TIMESTAMPTZ NULL         认领租约到期（claim-lease，§2.7）
- ``cleanup_run_id``      VARCHAR(36)  NULL         认领者 run uuid（完成更新守卫）

为何 5 列而非新表：清理状态是 ``user_voices`` 行的生命周期属性（与
``expired_at`` 软删同源），独立表反而要 join。与 PR2 的 reservation 表不同
——那是独立的成本闸事实，这里是音色行自身的清理态。

4.3b-A 只做 schema + ORM + 守卫；core / sweeper / CLI 不动。
本 migration 暂不部署、暂不 ``alembic upgrade`` —— 待 Phase 4.3 部署阶段统一上线。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "033_user_voice_cleanup_tracking"
down_revision: Union[str, None] = "032_express_clone_reservations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # backoff / give-up
    op.add_column(
        "user_voices",
        sa.Column(
            "cleanup_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "user_voices",
        sa.Column("cleanup_retry_after", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "user_voices",
        sa.Column("cleanup_last_error", sa.String(length=200), nullable=True),
    )
    # 并发 claim-lease（spec §2.7）
    op.add_column(
        "user_voices",
        sa.Column("cleanup_claim_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "user_voices",
        sa.Column("cleanup_run_id", sa.String(length=36), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_voices", "cleanup_run_id")
    op.drop_column("user_voices", "cleanup_claim_until")
    op.drop_column("user_voices", "cleanup_last_error")
    op.drop_column("user_voices", "cleanup_retry_after")
    op.drop_column("user_voices", "cleanup_attempts")
