"""Phase 4 claim binding — anonymous_preview_records.claim_user_id（owner 列 + 索引）.

Revision ID: 040_anonymous_preview_claim_owner
Revises: 039_smart_clone_carryover
Create Date: 2026-06-15

plan 2026-06-15-anonymous-preview-claim-binding-plan.md §4 / §9 T1.

匿名预览→登录认领（Model A 元数据桥）的正式 owner 列。认领时 ``POST
/gateway/anonymous-preview/claim`` 对本表做条件 ``UPDATE ... SET
claim_user_id=:user RETURNING preview_id``（plan §5.2）。
``AnonymousSession.claim_user_id``（migration 035 已建）做一次性认领锁；
本列做 owner + "按 user 列出已认领预览" 查询真源（不靠 JSON audit）。

bare nullable UUID（无 FK，与 035 的 anonymous_sessions.claim_user_id 同款）。
anonymous_preview_records 是 035 新建的冷表（匿名漏斗默认 OFF），普通事务内
``CREATE INDEX`` 即可——无需 035 那套 jobs 热表的 CONCURRENTLY/autocommit_block。

纯加列 + 索引，无约束/数据变更；默认 NULL → 既有 record 行 byte-identical inert。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "040_anonymous_preview_claim_owner"
down_revision: Union[str, None] = "039_smart_clone_carryover"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "anonymous_preview_records",
        sa.Column(
            "claim_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    # owner 反查「按 user 列出已认领预览」。plain btree（非 partial）以与 ORM
    # __table_args__ 的 Index 谓词保持一致，避免 create_all() vs alembic 的 schema 漂移。
    op.create_index(
        "ix_anon_preview_records_claim_user_id",
        "anonymous_preview_records",
        ["claim_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_anon_preview_records_claim_user_id",
        table_name="anonymous_preview_records",
    )
    op.drop_column("anonymous_preview_records", "claim_user_id")
