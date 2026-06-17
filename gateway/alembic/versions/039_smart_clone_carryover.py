"""P3e D-C: smart_clone_reservations.carryover_applied_to_task_id（预览→完整 600 结转 single-use 闸）.

Revision ID: 039_smart_clone_carryover
Revises: 038_smart_clone_created_at_index
Create Date: 2026-06-15

plan 2026-06-15-smart-clone-600-minute-offset-plan.md §4.6（D-C）。智能版预览
（扣 600 克隆点）转完整任务时，预览的 600 **结转抵扣**进完整任务分钟，使总扣
= ``max(600, 分钟×100)``（与直接做完整一致）。

``carryover_applied_to_task_id`` 是 **single-use 闸**：一条预览 reservation 的
600 结转额度最多被**一个**完整任务消费。完整任务终态结算时对预览 reservation 行
做条件 ``UPDATE ... SET carryover_applied_to_task_id=:F WHERE id=:preview_resv
AND status='captured' AND user_id=:F_user AND carryover_applied_to_task_id IS
NULL``（原子，与分钟 capture 同事务）。rowcount=1 → 抵 600；否则不重复抵。

NULL = 未被任何完整任务结转（默认；与既有行为 byte-identical inert）。

纯加列 + 索引，无约束/数据变更；默认 NULL → 既有 reservation 不受影响。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "039_smart_clone_carryover"
down_revision: Union[str, None] = "038_smart_clone_created_at_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # single-use 结转闸：哪个完整任务消费了本预览 reservation 的 600 结转额度。
    # NULL = 未结转（默认）。落定时间/审计经 ledger（capture 行）+ 本列双查。
    op.add_column(
        "smart_clone_reservations",
        sa.Column(
            "carryover_applied_to_task_id",
            sa.String(length=64),
            nullable=True,
        ),
    )
    # admin / analytics 反查「哪个完整任务消费了某结转额度」+ partial（只索引已结转行，
    # 表多为 NULL，省空间）。single-use 消费本身走 PK（preview reservation id），不依赖本索引。
    op.create_index(
        "idx_smart_clone_reservation_carryover",
        "smart_clone_reservations",
        ["carryover_applied_to_task_id"],
        postgresql_where=sa.text("carryover_applied_to_task_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_smart_clone_reservation_carryover",
        table_name="smart_clone_reservations",
    )
    op.drop_column("smart_clone_reservations", "carryover_applied_to_task_id")
