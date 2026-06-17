"""P3e-4b: smart_clone_reservations.created_at 索引（全局 daily cap count 提速）.

Revision ID: 038_smart_clone_created_at_index
Revises: 037_smart_clone_reservations
Create Date: 2026-06-15

plan 2026-06-14-p3e2-preview-lane-design.md §8（P3e-4b）。智能版预览克隆全局反滥用
cap 真生效后，``count_global_smart_reservations_today`` 每次 reserve 都对
``smart_clone_reservations`` 做 ``created_at >= shanghai_day_start`` 的**全局**计数
（不带 status 过滤）。037 既有索引只有 ``(user_id, status, created_at)``（前导列
user_id，不服务全局 created_at 范围扫）与 reserved 的 partial ``expires_at``，daily
全局查询无合适索引 → 表积累后会把反滥用闸本身打成 DB 热点（CodeX P3e-4b MEDIUM）。

补一个 ``created_at`` btree 索引服务该范围查询。inflight 全局计数
（``status=reserved AND expires_at>=now``）已由 037 的
``idx_smart_clone_reservation_ttl_pending``（partial on expires_at where
status='reserved'）覆盖，无需新增。

纯加索引，无数据/约束变更；默认 inert 特性不受影响。
"""

from typing import Sequence, Union

from alembic import op


revision: str = "038_smart_clone_created_at_index"
down_revision: Union[str, None] = "037_smart_clone_reservations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "idx_smart_clone_reservation_created_at",
        "smart_clone_reservations",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_smart_clone_reservation_created_at",
        table_name="smart_clone_reservations",
    )
